"""Live Google Chat path through the public handler.

Selected with `-m teams`, not by the unit addopts.
"""

from __future__ import annotations

import time
import uuid

import pytest

from tests.live_support import (
    ACK_TEXT,
    post_chat_message,
    read_follow_up,
    require_chat_thread,
)

pytestmark = [pytest.mark.teams, pytest.mark.timeout(300)]


def test_message_ack_then_reads_the_follow_up(live_env: dict[str, str]) -> None:
    if not live_env.get("GOOGLE_CLOUD_PROJECT"):
        pytest.skip("GOOGLE_CLOUD_PROJECT is not set")
    space, thread = require_chat_thread(live_env)
    question = "What is the max LTV for an owner-occupied residential mortgage?"
    started = time.time()
    reply = post_chat_message(
        question,
        thread=thread,
        space=space,
        user="users/live",
        message_name=f"{thread}/messages/{uuid.uuid4().hex}",
    )
    assert reply["thread"] == {"name": thread}
    assert reply["text"] == ACK_TEXT
    follow_up = read_follow_up(space=space, thread=thread, not_before=started)
    assert follow_up != ACK_TEXT
    assert "CA-{YYYYMMDD}-{unix_ms}" in follow_up
    assert "CA-20260115-1736899200123" in follow_up
