"""Live Google Chat path through the public handler.

Selected with `-m teams`, not by the unit addopts.
"""

from __future__ import annotations

import uuid

import pytest

from tests.live_support import post_chat_message

pytestmark = [pytest.mark.teams, pytest.mark.timeout(300)]


def test_message_calls_deployed_engine(live_env: dict[str, str]) -> None:
    if not live_env.get("GOOGLE_CLOUD_PROJECT"):
        pytest.skip("GOOGLE_CLOUD_PROJECT is not set")
    space = "spaces/live"
    thread = f"{space}/threads/{uuid.uuid4().hex}"
    question = "What is the max LTV for an owner-occupied residential mortgage?"
    reply = post_chat_message(
        question,
        thread=thread,
        space=space,
        user="users/live",
    )
    assert reply["thread"] == {"name": thread}
    assert str(reply["text"]).strip()
