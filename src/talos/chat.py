from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol, TextIO

from talos.constants import (
    DEFAULT_CHAT_MODEL,
    DEFAULT_CORPUS,
    DEFAULT_INSTRUCTIONS_RELATIVE,
)
from talos.env import repo_root
from talos.errors import TalosError
from talos.gemini import candidate_text, generate_content_url, retrieval_tool

CHAT_TIMEOUT_SECONDS = 180.0
GENERATE_RETRY_DELAYS = (2.0, 4.0, 8.0)
WAIT_LABEL = "Waiting for agent…"
_QUIT = frozenset({"/quit", "/exit", "quit", "exit"})
_SPINNER = "|/-\\"


@dataclass(frozen=True)
class ChatConfig:
    project: str
    location: str
    corpus_name: str
    model: str = DEFAULT_CHAT_MODEL
    instructions: str = ""
    dry_run: bool = False
    timeout: float = CHAT_TIMEOUT_SECONDS


@dataclass
class ChatTurn:
    text: str
    contents: list[dict[str, object]] = field(default_factory=list)


class WaitIndicator(Protocol):
    def start(self) -> None: ...

    def stop(self) -> None: ...


class NullWait:
    def start(self) -> None:
        return

    def stop(self) -> None:
        return


class TtyWaitIndicator:
    def __init__(
        self,
        stream: TextIO,
        *,
        enabled: bool | None = None,
        label: str = WAIT_LABEL,
        interval: float = 0.1,
    ) -> None:
        self._stream = stream
        self._enabled = stream.isatty() if enabled is None else enabled
        self._label = label
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self._enabled or self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=1.0)
        if self._enabled:
            width = len(self._label) + 4
            self._stream.write("\r" + " " * width + "\r")
            self._stream.flush()

    def _spin(self) -> None:
        index = 0
        while True:
            frame = _SPINNER[index % len(_SPINNER)]
            self._stream.write(f"\r{self._label} {frame}")
            self._stream.flush()
            index += 1
            if self._stop.wait(self._interval):
                return


class ChatModel(Protocol):
    def generate(self, *, contents: list[dict[str, object]], system: str) -> str: ...


class GeminiChatModel:
    def __init__(self, rest: object, config: ChatConfig) -> None:
        self._rest = rest
        self._config = config

    def generate(self, *, contents: list[dict[str, object]], system: str) -> str:
        url = generate_content_url(
            self._config.project, self._config.location, self._config.model
        )
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": contents,
            "tools": [retrieval_tool(self._config.corpus_name)],
            "generationConfig": {"temperature": 0},
        }
        delays = iter(GENERATE_RETRY_DELAYS)
        while True:
            response = self._rest.request(  # type: ignore[attr-defined]
                "POST", url, json_body=body, timeout=self._config.timeout
            )
            if response.status_code != 429:
                break
            try:
                delay = next(delays)
            except StopIteration:
                break
            time.sleep(delay)
        if response.status_code >= 400:
            raise TalosError(
                f"agent generateContent failed: HTTP {response.status_code} {response.text}",
                exit_code=1,
            )
        payload = response.json if isinstance(response.json, dict) else {}
        text = candidate_text(payload)
        if not text.strip():
            raise TalosError("agent generateContent returned empty text", exit_code=1)
        return text


def load_instructions(path: str | None = None) -> str:
    file = repo_root() / (path or DEFAULT_INSTRUCTIONS_RELATIVE)
    try:
        return file.read_text(encoding="utf-8")
    except OSError as exc:
        raise TalosError(
            f"Cannot read instructions {file}: {exc}", exit_code=1
        ) from exc


def run_chat(
    config: ChatConfig,
    question: str | None,
    *,
    interactive: bool,
    model: ChatModel | None = None,
    wait: WaitIndicator | None = None,
    prompt: Callable[[str], str] | None = None,
    echo: Callable[..., None] | None = None,
) -> None:
    if echo is None:
        echo = _default_echo
    if config.dry_run:
        echo("dry-run chat")
        echo(f"project: {config.project}")
        echo(f"location: {config.location}")
        echo(f"model: {config.model}")
        echo(f"corpus: {config.corpus_name or DEFAULT_CORPUS}")
        if question:
            echo(f"question: {question}")
        return
    chat = model or _default_model(config)
    if wait is None:
        wait = TtyWaitIndicator(sys.stderr)
    system = config.instructions or load_instructions()
    if interactive:
        _run_repl(
            chat,
            system,
            wait=wait,
            prompt=prompt or _default_prompt,
            echo=echo,
        )
        return
    text = (question or "").strip()
    if not text:
        raise TalosError("question is required", exit_code=1)
    turn = _ask_waiting(chat, system, text, [], wait)
    echo(turn.text)


def _default_model(config: ChatConfig) -> GeminiChatModel:
    from talos.rest import RequestsRest

    return GeminiChatModel(RequestsRest(), config)


def _ask_waiting(
    model: ChatModel,
    system: str,
    question: str,
    history: list[dict[str, object]],
    wait: WaitIndicator,
) -> ChatTurn:
    contents = [*history, {"role": "user", "parts": [{"text": question}]}]
    wait.start()
    try:
        text = model.generate(contents=contents, system=system)
    finally:
        wait.stop()
    updated = [
        *contents,
        {"role": "model", "parts": [{"text": text}]},
    ]
    return ChatTurn(text=text, contents=updated)


def _run_repl(
    model: ChatModel,
    system: str,
    *,
    wait: WaitIndicator,
    prompt: Callable[[str], str],
    echo: Callable[..., None],
) -> None:
    history: list[dict[str, object]] = []
    while True:
        try:
            line = prompt("> ")
        except EOFError:
            return
        except KeyboardInterrupt:
            echo("", err=True)
            return
        question = line.strip()
        if not question:
            continue
        if question.lower() in _QUIT:
            return
        try:
            turn = _ask_waiting(model, system, question, history, wait)
        except KeyboardInterrupt:
            echo("", err=True)
            continue
        except TalosError as exc:
            echo(str(exc), err=True)
            continue
        echo(turn.text)
        history = turn.contents


def _default_echo(message: object = "", err: bool = False, **_: object) -> None:
    stream = sys.stderr if err else sys.stdout
    text = "" if message is None else str(message)
    stream.write(text if text.endswith("\n") else f"{text}\n")
    stream.flush()


def _default_prompt(label: str) -> str:
    sys.stderr.write(label)
    sys.stderr.flush()
    line = sys.stdin.readline()
    if line == "":
        raise EOFError
    return line.rstrip("\n")
