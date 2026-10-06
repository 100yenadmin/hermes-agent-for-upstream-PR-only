"""Per-child reasoning effort contracts for delegate_task and the public lifecycle."""

import unittest
from unittest.mock import MagicMock, patch

from tests.tools.test_delegate import _make_mock_parent
from tools.delegate_tool import _build_child_agent


class TestDelegationReasoningEffort(unittest.TestCase):
    @patch("tools.delegate_tool._load_config")
    @patch("run_agent.AIAgent")
    def test_public_lifecycle_override_beats_delegation_config(self, MockAgent, mock_cfg):
        """An explicit public lifecycle effort is exact for this child only."""
        mock_cfg.return_value = {"max_iterations": 50, "reasoning_effort": "low"}
        MockAgent.return_value = MagicMock()
        parent = _make_mock_parent()
        parent.reasoning_config = {"enabled": True, "effort": "xhigh"}

        _build_child_agent(
            task_index=0,
            goal="test",
            context=None,
            toolsets=None,
            model=None,
            max_iterations=50,
            parent_agent=parent,
            task_count=1,
            override_reasoning_effort="high",
        )
        call_kwargs = MockAgent.call_args[1]
        self.assertEqual(call_kwargs["reasoning_config"], {"enabled": True, "effort": "high"})

    @patch("tools.delegate_tool._load_config")
    @patch("run_agent.AIAgent")
    def test_override_none_disables_child_thinking(self, MockAgent, mock_cfg):
        """override_reasoning_effort='none' disables thinking for the child."""
        mock_cfg.return_value = {"max_iterations": 50, "reasoning_effort": ""}
        MockAgent.return_value = MagicMock()
        parent = _make_mock_parent()
        parent.reasoning_config = {"enabled": True, "effort": "high"}

        _build_child_agent(
            task_index=0, goal="test", context=None, toolsets=None,
            model=None, max_iterations=50, parent_agent=parent,
            task_count=1, override_reasoning_effort="none",
        )
        call_kwargs = MockAgent.call_args[1]
        self.assertEqual(call_kwargs["reasoning_config"], {"enabled": False})


class TestChildReasoningEffortNormalization(unittest.TestCase):
    """Model-facing reasoning_effort param normalization."""

    def test_valid_levels_pass_through_lowercased(self):
        from tools.delegate_tool import _normalize_child_reasoning_effort
        self.assertEqual(_normalize_child_reasoning_effort("HIGH"), "high")
        self.assertEqual(_normalize_child_reasoning_effort(" xhigh "), "xhigh")
        self.assertEqual(_normalize_child_reasoning_effort("none"), "none")

    def test_empty_and_none_inherit(self):
        from tools.delegate_tool import _normalize_child_reasoning_effort
        self.assertIsNone(_normalize_child_reasoning_effort(None))
        self.assertIsNone(_normalize_child_reasoning_effort(""))
        self.assertIsNone(_normalize_child_reasoning_effort("   "))

    def test_unknown_degrades_to_inherit(self):
        from tools.delegate_tool import _normalize_child_reasoning_effort
        self.assertIsNone(_normalize_child_reasoning_effort("extreme"))
        self.assertIsNone(_normalize_child_reasoning_effort("42"))

    def test_schema_exposes_reasoning_effort(self):
        from tools.delegate_tool import DELEGATE_TASK_SCHEMA
        props = DELEGATE_TASK_SCHEMA["parameters"]["properties"]
        self.assertIn("reasoning_effort", props)
        task_props = props["tasks"]["items"]["properties"]
        self.assertIn("reasoning_effort", task_props)
        # Enum stays in sync with the constants ladder (+ "none").
        from hermes_constants import VALID_REASONING_EFFORTS
        expected = set(VALID_REASONING_EFFORTS) | {"none"}
        self.assertEqual(set(props["reasoning_effort"]["enum"]), expected)
        self.assertEqual(set(task_props["reasoning_effort"]["enum"]), expected)

