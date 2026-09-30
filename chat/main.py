"""Google Chat HTTPS handler for the credit officer on Agent Runtime."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Protocol
from urllib.parse import quote

CLASS_METHOD = "async_stream_query"
STREAM_QUERY_TIMEOUT = 180.0
_API_VERSION = "v1beta1"
_RESOURCE = re.compile(r"^projects/([^/]+)/locations/([^/]+)/reasoningEngines/([^/]+)$")
_ENGINE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
# Sessions API: <=63, [a-z0-9-], starts with a letter, ends with a letter or number.
_SESSION_FOLD = re.compile(r"[^a-z0-9]+")
_SESSION_OK = re.compile(r"^[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_MAX_BODY = 1_000_000
_CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
_CHAT_BOT_SCOPE = "https://www.googleapis.com/auth/chat.bot"
_RETRY_STATUSES = frozenset({429, 500, 502, 503})
_RETRY_ATTEMPTS = 4
_HANDLER_ENV = (
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_CLOUD_LOCATION",
    "REASONING_ENGINE",
)
# Public host. Audience matches Cloud Run custom_audiences. No trailing slash.
CHAT_URL = "https://credit-policy.ai.lab5.ca"
ACK_TEXT = "Request received."
ENQUEUE_FAILED_TEXT = "Enqueue failed."
HELP_TEXT = (
    "A credit application id is required. "
    "Format: CA-{YYYYMMDD}-{unix_ms}. "
    "Example: CA-20260115-1736899200123."
)
OFFICER_ERROR_TEXT = "The credit officer could not complete that request."
CHAT_SYSTEM_ACCOUNT = "chat@system.gserviceaccount.com"
_JUDGE_PATH = "/tasks/judge"
_APPLICATION_ID = re.compile(r"(?<![A-Za-z0-9])CA-\d{8}-\d+(?![A-Za-z0-9])")
_TASK_ID_CHARS = re.compile(r"[^A-Za-z0-9_-]")
_CHAT_API = "https://chat.googleapis.com/v1"


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
        attempts: int = 4,
    ) -> RestResponse: ...


class AgentRuntime(Protocol):
    def stream_query(self, *, user_id: str, session_id: str, message: str) -> str: ...


class TaskEnqueuer(Protocol):
    def enqueue(self, *, task_id: str, payload: Mapping[str, Any]) -> None: ...


class ChatPoster(Protocol):
    def create_message(
        self,
        *,
        parent: str,
        thread: str,
        text: str,
        request_id: str,
    ) -> Mapping[str, Any]: ...


def queue_name(project: str) -> str:
    return f"{project}-chat"


def tasks_invoker_email(project: str) -> str:
    return f"chat-tasks@{project}.iam.gserviceaccount.com"


def application_ids(text: str) -> list[str]:
    """Distinct CA-{YYYYMMDD}-{unix_ms} ids in first-seen order."""
    found: list[str] = []
    seen: set[str] = set()
    for match in _APPLICATION_ID.finditer(text):
        value = match.group(0)
        if value in seen:
            continue
        seen.add(value)
        found.append(value)
    return found


def task_id_from_message(message_name: str) -> str:
    """Cloud Tasks id derived from the Chat message resource name."""
    cleaned = _TASK_ID_CHARS.sub("_", message_name.strip())
    if not cleaned or len(cleaned) > 500:
        raise HandlerError("Chat message name cannot be a task id.", exit_code=1)
    return cleaned


def outcome_request_id(message_name: str, outcome: str) -> str:
    """Stable Chat requestId for one message and one outcome."""
    digest = hashlib.sha256(f"{outcome}\n{message_name}".encode()).hexdigest()
    return f"{outcome}-{digest[:40]}"


def list_ids_text(ids: list[str]) -> str:
    listed = ", ".join(ids)
    return f"More than one credit application id matched: {listed}. No judgement."


def task_http_body(
    config: ChatHandlerConfig, task_id: str, payload: Mapping[str, Any]
) -> dict[str, Any]:
    project = config.project
    location = config.location
    queue = queue_name(project)
    name = f"projects/{project}/locations/{location}/queues/{queue}/tasks/{task_id}"
    encoded = base64.b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).decode("ascii")
    return {
        "task": {
            "name": name,
            "httpRequest": {
                "httpMethod": "POST",
                "url": f"{CHAT_URL}{_JUDGE_PATH}",
                "headers": {"Content-Type": "application/json"},
                "body": encoded,
                "oidcToken": {
                    "serviceAccountEmail": tasks_invoker_email(project),
                    "audience": CHAT_URL,
                },
            },
        }
    }


def session_id(space: str, thread: str) -> str:
    """Stable session id for one Chat space and thread."""
    folded = _SESSION_FOLD.sub("-", f"{space} {thread}".lower())
    if _SESSION_OK.fullmatch(folded):
        return folded
    digest = hashlib.sha256(f"{space}\n{thread}".encode()).hexdigest()
    return f"c{digest[:62]}"


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
        self,
        credentials: Any | None = None,
        *,
        quota_project: str | None = None,
        scopes: list[str] | None = None,
    ) -> None:
        self._credentials = credentials
        self._quota_project = quota_project
        self._scopes = scopes or [_CLOUD_PLATFORM_SCOPE]

    def request(
        self,
        method: str,
        url: str,
        *,
        json_body: Any | None = None,
        timeout: float = 60.0,
        attempts: int = _RETRY_ATTEMPTS,
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
        tries = attempts if attempts > 0 else 1
        for attempt in range(tries):
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
                if attempt == tries - 1:
                    break
                time.sleep(float(2**attempt))
                continue
            if response.status_code not in _RETRY_STATUSES or attempt == tries - 1:
                return _rest_response(response)
            time.sleep(float(2**attempt))
        raise HandlerError(f"request failed: {last_error}", exit_code=1) from last_error

    def _ready_credentials(self) -> Any:
        if self._credentials is None:
            import google.auth

            credentials, _detected = google.auth.default(
                scopes=self._scopes,
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
        text = model_text(response.text)
        if text:
            return text
        error = stream_error(response.text)
        if error:
            raise HandlerError(f"streamQuery failed: {error}")
        return ""


def stream_error(payload: str) -> str:
    """Last error from an HTTP 200 streamQuery body that carries no model text."""
    messages: list[str] = []
    for event in _stream_objects(payload):
        if not isinstance(event, dict):
            continue
        for key in ("error_message", "errorMessage"):
            value = event.get(key)
            if isinstance(value, str) and value.strip():
                messages.append(value.strip())
                break
        else:
            code = event.get("code", event.get("errorCode", event.get("error_code")))
            message = event.get("message")
            if (
                code is not None
                and isinstance(message, str)
                and message.strip()
                and "content" not in event
            ):
                messages.append(message.strip())
    return messages[-1] if messages else ""


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


class CloudTasksEnqueuer:
    def __init__(
        self, config: ChatHandlerConfig, client: RestClient | None = None
    ) -> None:
        self._config = config
        self._client = client

    def enqueue(self, *, task_id: str, payload: Mapping[str, Any]) -> None:
        if self._client is None:
            self._client = RequestsRest(quota_project=self._config.project)
        project = self._config.project
        location = self._config.location
        url = (
            "https://cloudtasks.googleapis.com/v2/"
            f"projects/{project}/locations/{location}/"
            f"queues/{queue_name(project)}/tasks"
        )
        response = self._client.request(
            "POST",
            url,
            json_body=task_http_body(self._config, task_id, payload),
            timeout=5.0,
            attempts=1,
        )
        # A second delivery of the same message id is already queued.
        if response.status_code == 409:
            return
        raise_for_status(response, "Cloud Tasks enqueue")


class RestChatPoster:
    """spaces.messages.create as credit-policy-agent with the chat.bot scope."""

    def __init__(
        self, config: ChatHandlerConfig, client: RestClient | None = None
    ) -> None:
        self._config = config
        self._client = client

    def create_message(
        self,
        *,
        parent: str,
        thread: str,
        text: str,
        request_id: str,
    ) -> Mapping[str, Any]:
        if self._client is None:
            # A quota-project header requires serviceusage.services.use.
            # credit-policy-agent does not have that permission, and a
            # chat.bot token with the header makes spaces.messages.create 403.
            self._client = RequestsRest(scopes=[_CHAT_BOT_SCOPE])
        url = (
            f"{_CHAT_API}/{parent}/messages"
            f"?requestId={quote(request_id, safe='')}"
            "&messageReplyOption=REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
        )
        response = self._client.request(
            "POST",
            url,
            json_body={"text": text, "thread": {"name": thread}},
            timeout=30.0,
        )
        raise_for_status(response, "spaces.messages.create")
        if isinstance(response.json, dict):
            return response.json
        return {}


def http_reply(body: bytes, tasks: TaskEnqueuer) -> tuple[int, dict[str, Any]]:
    try:
        event = json.loads(body)
    except json.JSONDecodeError:
        return 400, {"text": "Invalid JSON."}
    if not isinstance(event, dict):
        return 400, {"text": "Chat event must be a JSON object."}
    if event.get("type") != "MESSAGE":
        return 200, {}
    message = event.get("message")
    if not isinstance(message, dict):
        return _enqueue_failed()
    sender = message.get("sender")
    if isinstance(sender, dict) and sender.get("type") == "BOT":
        return 200, {}
    space, thread = _space_and_thread(event)
    message_name = _message_name(message)
    if not message_name or not space or not thread:
        return _enqueue_failed()
    try:
        tasks.enqueue(task_id=task_id_from_message(message_name), payload=event)
    except HandlerError:
        return _enqueue_failed()
    return 200, {"text": ACK_TEXT, "thread": {"name": thread}}


def http_judge(
    body: bytes,
    runtime: AgentRuntime,
    poster: ChatPoster,
    headers: Mapping[str, str],
    project: str,
) -> tuple[int, dict[str, Any]]:
    email = caller_email(headers)
    if email == CHAT_SYSTEM_ACCOUNT or email != tasks_invoker_email(project):
        return 403, {"text": "Forbidden."}
    try:
        event = json.loads(body)
    except json.JSONDecodeError:
        return 400, {"text": "Invalid JSON."}
    if not isinstance(event, dict):
        return 400, {"text": "Chat event must be a JSON object."}
    try:
        judge_event(event, runtime, poster)
    except Exception as exc:
        print(
            f"judge failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        if not isinstance(exc, HandlerError):
            raise
        return 500, {}
    return 200, {}


def judge_event(
    event: Mapping[str, Any], runtime: AgentRuntime, poster: ChatPoster
) -> None:
    message = event.get("message")
    if not isinstance(message, dict):
        raise HandlerError("Chat event is missing the message.")
    space, thread = _space_and_thread(event)
    message_name = _message_name(message)
    if not message_name or not space or not thread:
        raise HandlerError("Chat event is missing the message, space, or thread.")
    question = _question(message)
    ids = application_ids(question)
    if not ids:
        _post(
            poster,
            space=space,
            thread=thread,
            text=HELP_TEXT,
            request_id=outcome_request_id(message_name, "help"),
        )
        return
    if len(ids) > 1:
        _post(
            poster,
            space=space,
            thread=thread,
            text=list_ids_text(ids),
            request_id=outcome_request_id(message_name, "list"),
        )
        return
    user = _user_id(event)
    if not user:
        _post_officer_failure(poster, space, thread, message_name)
        return
    try:
        answer = runtime.stream_query(
            user_id=user,
            session_id=session_id(space, thread),
            message=question,
        )
    except HandlerError:
        _post_officer_failure(poster, space, thread, message_name)
        return
    if not answer.strip():
        _post_officer_failure(poster, space, thread, message_name)
        return
    _post(
        poster,
        space=space,
        thread=thread,
        text=answer,
        request_id=outcome_request_id(message_name, "judgement"),
    )


def serve(
    runtime: AgentRuntime,
    host: str,
    port: int,
    *,
    tasks: TaskEnqueuer,
    poster: ChatPoster,
    project: str,
) -> ThreadingHTTPServer:
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
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if path == _JUDGE_PATH:
                status, payload = http_judge(
                    raw, runtime, poster, self.headers, project
                )
            else:
                status, payload = http_reply(raw, tasks)
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
    server = serve(
        RestAgentRuntime(config),
        host,
        port,
        tasks=CloudTasksEnqueuer(config),
        poster=RestChatPoster(config),
        project=config.project,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


def verify_google_id_token(token: str, audience: str) -> Mapping[str, Any]:
    """Verify a Google-signed ID token. Audience must match."""
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    claims = id_token.verify_oauth2_token(token, google_requests.Request(), audience)
    if not isinstance(claims, Mapping):
        raise ValueError("ID token claims must be a mapping.")
    return claims


def _authorization_bearer(headers: Mapping[str, str]) -> str:
    authorization = headers.get("Authorization") or ""
    if not isinstance(authorization, str):
        return ""
    scheme, _, token = authorization.strip().partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return token.strip()


def caller_email(headers: Mapping[str, str]) -> str:
    """Email on a verified Google-signed ID token. Unsigned bearer is not a caller."""
    from google.auth.exceptions import GoogleAuthError

    token = _authorization_bearer(headers)
    if not token:
        return ""
    try:
        claims = verify_google_id_token(token, CHAT_URL)
    except ValueError, GoogleAuthError:
        return ""
    email = claims.get("email")
    if not isinstance(email, str) or not email.strip():
        return ""
    return email.strip().lower()


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


def _message_name(message: Mapping[str, Any]) -> str:
    name = message.get("name")
    if isinstance(name, str):
        return name.strip()
    return ""


def _enqueue_failed() -> tuple[int, dict[str, Any]]:
    return 500, {"text": ENQUEUE_FAILED_TEXT}


def _post(
    poster: ChatPoster,
    *,
    space: str,
    thread: str,
    text: str,
    request_id: str,
) -> None:
    poster.create_message(
        parent=space,
        thread=thread,
        text=text,
        request_id=request_id,
    )


def _post_officer_failure(
    poster: ChatPoster, space: str, thread: str, message_name: str
) -> None:
    _post(
        poster,
        space=space,
        thread=thread,
        text=OFFICER_ERROR_TEXT,
        request_id=outcome_request_id(message_name, "failure"),
    )


def _question(message: Mapping[str, Any]) -> str:
    argument = message.get("argumentText")
    if isinstance(argument, str) and argument.strip():
        return argument.strip()
    text = message.get("text")
    if isinstance(text, str):
        return text.strip()
    return ""


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
