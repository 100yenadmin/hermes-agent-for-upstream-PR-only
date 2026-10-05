"""Manual gateway /refine requests carry explicit intent to the review fork."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway.slash_commands_goals import GatewayGoalCommandsMixin


@pytest.mark.asyncio
@pytest.mark.parametrize("args", ["", "save the workflow"])
async def test_refine_marks_bare_and_focused_requests_explicit(args):
    agent = SimpleNamespace(
        _session_messages=[{"role": "user", "content": "hello"}],
        valid_tool_names={"skill_manage"},
        _spawn_background_review=MagicMock(),
    )
    runner = GatewayGoalCommandsMixin()
    runner._running_agents = {}
    runner._session_key_for_source = lambda _source: "conversation"
    runner._cached_agent_for = lambda _key: agent
    event = SimpleNamespace(source=object(), get_command_args=lambda: args)

    await runner._handle_refine_command(event)

    agent._spawn_background_review.assert_called_once_with(
        messages_snapshot=agent._session_messages,
        review_memory=True,
        review_skills=True,
        focus=args or None,
        explicit=True,
    )
