"""Live Google Chat path. Selected with `-m teams`, not by the unit addopts."""

from __future__ import annotations

import pytest

from talos.google_chat import ChatHandlerConfig, RestAgentRuntime, handle_chat_event

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
