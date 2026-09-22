from __future__ import annotations

import pytest

from talos.chat import ChatConfig, TtyWaitIndicator, run_chat
from talos.errors import TalosError

pytestmark = pytest.mark.unit


class RecordingModel:
    def __init__(self, answers: list[str]) -> None:
        self._answers = list(answers)
        self.contents: list[list[dict[str, object]]] = []

    def generate(self, *, contents: list[dict[str, object]], system: str) -> str:
        self.contents.append(contents)
        if not self._answers:
            return ""
        return self._answers.pop(0)


class RecordingWait:
    def __init__(self) -> None:
        self.events: list[str] = []

    def start(self) -> None:
        self.events.append("start")

    def stop(self) -> None:
        self.events.append("stop")


def _config(**overrides: object) -> ChatConfig:
    values: dict[str, object] = dict(
        project="lab5-gemini-dev1",
        location="us-east1",
        corpus_name="projects/p/locations/us-east1/ragCorpora/1",
        instructions="Answer from the corpus.",
    )
    values.update(overrides)
    return ChatConfig(**values)  # type: ignore[arg-type]


def test_dry_run_does_not_call_model() -> None:
    model = RecordingModel(["unused"])
    lines: list[str] = []
    run_chat(
        _config(dry_run=True),
        "What is max LTV?",
        interactive=False,
        model=model,
        echo=lambda message="", **_: lines.append(str(message)),
    )
    assert model.contents == []
    assert any("dry-run chat" in line for line in lines)


def test_one_shot_prints_answer() -> None:
    model = RecordingModel(["Max LTV is 80%."])
    lines: list[str] = []
    wait = RecordingWait()
    run_chat(
        _config(),
        "What is max LTV?",
        interactive=False,
        model=model,
        wait=wait,
        echo=lambda message="", **_: lines.append(str(message)),
    )
    assert lines == ["Max LTV is 80%."]
    assert wait.events == ["start", "stop"]
    assert model.contents[0][-1]["role"] == "user"


def test_empty_question_exits_1() -> None:
    with pytest.raises(TalosError, match="question is required") as exc:
        run_chat(_config(), "  ", interactive=False, model=RecordingModel(["x"]))
    assert exc.value.exit_code == 1


def test_repl_threads_prior_contents() -> None:
    model = RecordingModel(["80%.", "Owner-occupied."])
    prompts = iter(["What is max LTV?", "Which occupancy?", "/quit"])
    lines: list[str] = []
    run_chat(
        _config(),
        None,
        interactive=True,
        model=model,
        wait=RecordingWait(),
        prompt=lambda _label: next(prompts),
        echo=lambda message="", **_: lines.append(str(message)),
    )
    assert lines == ["80%.", "Owner-occupied."]
    second = model.contents[1]
    roles = [item["role"] for item in second]
    assert roles == ["user", "model", "user"]


def test_tty_wait_disabled_writes_nothing() -> None:
    class Stream:
        def __init__(self) -> None:
            self.buf = ""

        def isatty(self) -> bool:
            return False

        def write(self, text: str) -> None:
            self.buf += text

        def flush(self) -> None:
            return None

    stream = Stream()
    wait = TtyWaitIndicator(stream, enabled=False)  # type: ignore[arg-type]
    wait.start()
    wait.stop()
    assert stream.buf == ""
