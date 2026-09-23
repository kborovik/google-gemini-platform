"""Live Google Chat path. Selected with `-m teams`, not by the unit addopts."""

from __future__ import annotations

import importlib.util
import sys

import pytest

from docgen.env import repo_root


def _load_chat():
    path = repo_root() / "chat" / "main.py"
    spec = importlib.util.spec_from_file_location("chat_main", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["chat_main"] = module
    spec.loader.exec_module(module)
    return module


_chat = _load_chat()
ChatHandlerConfig = _chat.ChatHandlerConfig
RestAgentRuntime = _chat.RestAgentRuntime
handle_chat_event = _chat.handle_chat_event

pytestmark = [pytest.mark.teams, pytest.mark.timeout(300)]


def test_message_calls_deployed_engine(live_env: dict[str, str]) -> None:
    engine = live_env.get("REASONING_ENGINE", "")
    if not engine:
        pytest.skip("REASONING_ENGINE is not set")
    runtime = RestAgentRuntime(
        ChatHandlerConfig(
            project=live_env["GOOGLE_CLOUD_PROJECT"],
            location=live_env["GOOGLE_CLOUD_LOCATION"],
            reasoning_engine=engine,
        )
    )
    space = "spaces/live"
    thread = "spaces/live/threads/t1"
    reply = handle_chat_event(
        {
            "type": "MESSAGE",
            "user": {"name": "users/live", "type": "HUMAN"},
            "space": {"name": space},
            "message": {
                "text": "What is the max LTV for an owner-occupied residential mortgage?",
                "argumentText": (
                    "What is the max LTV for an owner-occupied residential mortgage?"
                ),
                "sender": {"name": "users/live", "type": "HUMAN"},
                "space": {"name": space},
                "thread": {"name": thread},
            },
        },
        runtime,
    )
    assert reply["thread"] == {"name": thread}
    assert str(reply["text"]).strip()
