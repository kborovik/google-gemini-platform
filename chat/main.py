"""Google Chat HTTPS handler for the credit officer on Agent Runtime."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Protocol

CLASS_METHOD = "async_stream_query"
STREAM_QUERY_TIMEOUT = 180.0
_API_VERSION = "v1beta1"
_RESOURCE = re.compile(r"^projects/([^/]+)/locations/([^/]+)/reasoningEngines/([^/]+)$")
_ENGINE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_MAX_BODY = 1_000_000
_CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
_RETRY_STATUSES = frozenset({429, 500, 502, 503})
_RETRY_ATTEMPTS = 4
_HANDLER_ENV = (
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_CLOUD_LOCATION",
    "REASONING_ENGINE",
)


class HandlerError(Exception):
    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True)
class ChatHandlerConfig:
    project: str
    location: str
    reasoning_engine: str


@dataclass
class RestResponse:
    status_code: int
    json: Any
    text: str


class RestClient(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        json_body: Any | None = None,
        timeout: float = 60.0,
    ) -> RestResponse: ...


class AgentRuntime(Protocol):
    def stream_query(self, *, user_id: str, session_id: str, message: str) -> str: ...


def session_id(space: str, thread: str) -> str:
    return f"{space} {thread}"


def stream_query_url(project: str, location: str, reasoning_engine: str) -> str:
    resource = reasoning_engine.strip()
    match = _RESOURCE.fullmatch(resource)
    if match:
        project, location, engine_id = match.groups()
    else:
        engine_id = resource
    if not project or not location or not _ENGINE_ID.fullmatch(engine_id):
        raise HandlerError(
            "REASONING_ENGINE must be an engine id or "
            "projects/{project}/locations/{location}/reasoningEngines/{id}.",
            exit_code=2,
        )
    return (
        f"https://{location}-aiplatform.googleapis.com/{_API_VERSION}/"
        f"projects/{project}/locations/{location}/reasoningEngines/{engine_id}:streamQuery"
    )


def stream_query_body(*, user_id: str, session_id: str, message: str) -> dict[str, Any]:
    # REST JSON name for the streamQuery class_method field.
    return {
        "classMethod": CLASS_METHOD,
        "input": {
            "user_id": user_id,
            "session_id": session_id,
            "message": message,
        },
    }


def config_from_env(env: Mapping[str, str]) -> ChatHandlerConfig:
    missing = [name for name in _HANDLER_ENV if not env.get(name)]
    if missing:
        names = ", ".join(missing)
        raise HandlerError(
            f"Google Cloud environment is not configured (missing {names}).",
            exit_code=2,
        )
    return ChatHandlerConfig(
        project=env["GOOGLE_CLOUD_PROJECT"],
        location=env["GOOGLE_CLOUD_LOCATION"],
        reasoning_engine=env["REASONING_ENGINE"],
    )


def bind_address(argv: list[str] | None, env: Mapping[str, str]) -> tuple[str, int]:
    parser = argparse.ArgumentParser(description="Google Chat handler")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args(argv)
    if args.port is not None:
        return args.host, args.port
    raw = env.get("PORT", "8080")
    try:
        port = int(raw)
    except (TypeError, ValueError) as exc:
        raise HandlerError(
            f"PORT must be an integer (got {raw}).", exit_code=2
        ) from exc
    return args.host, port


class RequestsRest:
    def __init__(
        self, credentials: Any | None = None, *, quota_project: str | None = None
    ) -> None:
        self._credentials = credentials
        self._quota_project = quota_project

    def request(
        self,
        method: str,
        url: str,
        *,
        json_body: Any | None = None,
        timeout: float = 60.0,
    ) -> RestResponse:
        import requests

        credentials = self._ready_credentials()
        headers = {"Content-Type": "application/json"}
        apply = getattr(credentials, "apply", None)
        if callable(apply):
            apply(headers)
        else:
            headers["Authorization"] = f"Bearer {credentials.token}"
        quota = getattr(credentials, "quota_project_id", None) or self._quota_project
        if isinstance(quota, str) and quota and "x-goog-user-project" not in headers:
            headers["x-goog-user-project"] = quota
        last_error: Exception | None = None
        for attempt in range(_RETRY_ATTEMPTS):
            try:
                response = requests.request(
                    method,
                    url,
                    headers=headers,
                    json=json_body,
                    timeout=timeout,
                )
            except requests.RequestException as exc:
                last_error = exc
                if attempt == _RETRY_ATTEMPTS - 1:
                    break
                time.sleep(float(2**attempt))
                continue
            if (
                response.status_code not in _RETRY_STATUSES
                or attempt == _RETRY_ATTEMPTS - 1
            ):
                return _rest_response(response)
            time.sleep(float(2**attempt))
        raise HandlerError(f"request failed: {last_error}", exit_code=1) from last_error

    def _ready_credentials(self) -> Any:
        if self._credentials is None:
            import google.auth

            credentials, _detected = google.auth.default(
                scopes=[_CLOUD_PLATFORM_SCOPE],
                quota_project_id=self._quota_project,
            )
            self._credentials = credentials
        credentials = self._credentials
        if not getattr(credentials, "valid", False):
            import google.auth.transport.requests

            credentials.refresh(google.auth.transport.requests.Request())
        token = getattr(credentials, "token", None)
        if not isinstance(token, str) or not token:
            raise HandlerError(
                "Google credentials did not yield an access token", exit_code=2
            )
        return credentials


def _rest_response(response: Any) -> RestResponse:
    body: Any = None
    text = response.text or ""
    if text:
        try:
            body = response.json()
        except ValueError:
            body = None
    return RestResponse(status_code=response.status_code, json=body, text=text)


def raise_for_status(response: RestResponse, action: str) -> None:
    if response.status_code < 400:
        return
    detail = response.text.strip() or str(response.json)
    if len(detail) > 500:
        detail = detail[:500] + "…"
    raise HandlerError(
        f"{action} failed: HTTP {response.status_code} {detail}", exit_code=1
    )


class RestAgentRuntime:
    def __init__(
        self, config: ChatHandlerConfig, client: RestClient | None = None
    ) -> None:
        self._config = config
        self._client = client

    def stream_query(self, *, user_id: str, session_id: str, message: str) -> str:
        if self._client is None:
            self._client = RequestsRest(quota_project=self._config.project)
        response = self._client.request(
            "POST",
            stream_query_url(
                self._config.project,
                self._config.location,
                self._config.reasoning_engine,
            ),
            json_body=stream_query_body(
                user_id=user_id, session_id=session_id, message=message
            ),
            timeout=STREAM_QUERY_TIMEOUT,
        )
        raise_for_status(response, "streamQuery")
        return model_text(response.text)


def model_text(payload: str) -> str:
    partials: list[str] = []
    finals: list[str] = []
    for event in _stream_objects(payload):
        piece, partial, author = _event_text(event)
        if not piece or author == "RetrievalAgent":
            continue
        if partial:
            partials.append(piece)
        else:
            finals.append(piece)
    if finals:
        return finals[-1].strip()
    return "".join(partials).strip()


def handle_chat_event(event: dict[str, Any], runtime: AgentRuntime) -> dict[str, Any]:
    if event.get("type") != "MESSAGE":
        return {}
    message = event.get("message")
    if not isinstance(message, dict):
        return _note(_thread_name(event), "Chat event is missing the message.")
    sender = message.get("sender")
    if isinstance(sender, dict) and sender.get("type") == "BOT":
        return {}
    user = _user_id(event)
    space, thread = _space_and_thread(event)
    if not user or not space or not thread:
        return _note(thread, "Chat event is missing the user, space, or thread.")
    question = _question(message)
    if not question:
        return _note(thread, "Send a question.")
    answer = runtime.stream_query(
        user_id=user,
        session_id=session_id(space, thread),
        message=question,
    )
    if not answer:
        answer = "The agent returned no text."
    return {"text": answer, "thread": {"name": thread}}


def http_reply(body: bytes, runtime: AgentRuntime) -> tuple[int, dict[str, Any]]:
    try:
        event = json.loads(body)
    except json.JSONDecodeError:
        return 400, {"text": "Invalid JSON."}
    if not isinstance(event, dict):
        return 400, {"text": "Chat event must be a JSON object."}
    try:
        return 200, handle_chat_event(event, runtime)
    except HandlerError as exc:
        return 200, _note(_thread_name(event), str(exc))


def serve(runtime: AgentRuntime, host: str, port: int) -> ThreadingHTTPServer:
    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length_header = self.headers.get("Content-Length")
            try:
                length = int(length_header or "0")
            except ValueError:
                length = -1
            if length < 0 or length > _MAX_BODY:
                self._send(400, {"text": "Invalid JSON."})
                return
            raw = self.rfile.read(length) if length else b""
            status, payload = http_reply(raw, runtime)
            self._send(status, payload)

        def log_message(self, format: str, *args: object) -> None:
            return

        def _send(self, status: int, payload: dict[str, Any]) -> None:
            encoded = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    return ThreadingHTTPServer((host, port), _Handler)


def main(argv: list[str] | None = None) -> int:
    try:
        host, port = bind_address(argv, os.environ)
        config = config_from_env(os.environ)
    except HandlerError as exc:
        print(exc, file=sys.stderr)
        return exc.exit_code
    server = serve(RestAgentRuntime(config), host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


def _user_id(event: Mapping[str, Any]) -> str:
    user = event.get("user")
    if isinstance(user, dict):
        name = user.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    message = event.get("message")
    if isinstance(message, dict):
        sender = message.get("sender")
        if isinstance(sender, dict):
            name = sender.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
    return ""


def _space_and_thread(event: Mapping[str, Any]) -> tuple[str, str]:
    message = event.get("message")
    message = message if isinstance(message, dict) else {}
    space_obj = message.get("space")
    if not isinstance(space_obj, dict):
        space_obj = event.get("space")
    thread_obj = message.get("thread")
    space = space_obj.get("name") if isinstance(space_obj, dict) else ""
    thread = thread_obj.get("name") if isinstance(thread_obj, dict) else ""
    space = space.strip() if isinstance(space, str) else ""
    thread = thread.strip() if isinstance(thread, str) else ""
    return space, thread


def _thread_name(event: Mapping[str, Any]) -> str:
    return _space_and_thread(event)[1]


def _question(message: Mapping[str, Any]) -> str:
    argument = message.get("argumentText")
    if isinstance(argument, str) and argument.strip():
        return argument.strip()
    text = message.get("text")
    if isinstance(text, str):
        return text.strip()
    return ""


def _note(thread: str, text: str) -> dict[str, Any]:
    reply: dict[str, Any] = {"text": text}
    if thread:
        reply["thread"] = {"name": thread}
    return reply


def _stream_objects(payload: str) -> list[Any]:
    stripped = payload.strip()
    if not stripped:
        return []
    if stripped.startswith("data:"):
        objects: list[Any] = []
        for line in payload.splitlines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data and data != "[DONE]":
                objects.extend(_stream_objects(data))
        return objects
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        return [parsed]
    decoder = json.JSONDecoder()
    objects = []
    index = 0
    length = len(stripped)
    while index < length:
        while index < length and stripped[index].isspace():
            index += 1
        if index >= length:
            break
        try:
            obj, end = decoder.raw_decode(stripped, index)
        except json.JSONDecodeError:
            break
        objects.append(obj)
        index = end
    return objects


def _event_text(event: Any) -> tuple[str, bool, str]:
    if isinstance(event, dict) and "output" in event and "content" not in event:
        output = event["output"]
        if isinstance(output, str):
            try:
                event = json.loads(output)
            except json.JSONDecodeError:
                return output, False, ""
        else:
            event = output
    if not isinstance(event, dict):
        return "", False, ""
    author = event.get("author")
    author = author if isinstance(author, str) else ""
    content = event.get("content")
    if isinstance(content, str):
        return content, event.get("partial") is True, author
    parts: Any = []
    if isinstance(content, dict):
        parts = content.get("parts") or []
    texts: list[str] = []
    if isinstance(parts, list):
        for part in parts:
            if not isinstance(part, dict):
                continue
            if any(
                key in part
                for key in ("function_call", "functionCall", "function_response")
            ):
                continue
            text = part.get("text")
            if isinstance(text, str) and text:
                texts.append(text)
    return "".join(texts), event.get("partial") is True, author


if __name__ == "__main__":
    sys.exit(main())
