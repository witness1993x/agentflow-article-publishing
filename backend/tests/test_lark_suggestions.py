"""Tests for the Lark Suggestions (Gate S) parity flow.

Covers:
* ``review_triggers._emit_lark_suggestion_list_card`` /
  ``_emit_lark_suggestion_review_card`` event emission.
* ``lark_callback._handle_suggestion_review`` /
  ``_handle_suggestion_apply`` / ``_handle_suggestion_dismiss`` /
  ``_handle_suggestion_list`` per-handler behaviour + auth fail-closed.
* ``web._LARK_COMMANDS`` registration of the four new commands.

Plan ref: docs/BLOGFLOW_TG_TO_LARK_PARITY.md §3.1 GAP-S.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from agentflow.agent_review import lark_callback
from agentflow.agent_review import triggers as review_triggers
from agentflow.agent_review import web as review_web
from agentflow.shared import bootstrap, memory


_OPERATOR = {"open_id": "ou_op_test", "name": "Tester"}


def _write_lark_operator(home: Path, open_id: str, actions: list[str]) -> None:
    """Seed ``lark_operators`` so ``is_authorized_open_id`` returns True."""
    review_dir = home / "review"
    review_dir.mkdir(parents=True, exist_ok=True)
    auth_path = review_dir / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "lark_operators": [
                    {"open_id": open_id, "name": "Tester", "actions": actions},
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


class _SuggestionTestCase(unittest.TestCase):
    """Common AGENTFLOW_HOME tmp + memory redirect setup."""

    def setUp(self) -> None:
        super().setUp()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.home = Path(self._tmpdir.name)
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(bootstrap, "AGENTFLOW_HOME", self.home))
        self.stack.enter_context(
            patch.object(memory, "MEMORY_DIR", self.home / "memory")
        )
        self.stack.enter_context(
            patch.object(memory, "EVENTS_PATH", self.home / "memory" / "events.jsonl")
        )
        bootstrap.ensure_user_dirs()

    def tearDown(self) -> None:
        self.stack.close()
        self._tmpdir.cleanup()
        super().tearDown()


# ---------------------------------------------------------------------------
# _emit_lark_suggestion_list_card
# ---------------------------------------------------------------------------


class EmitSuggestionListCardTests(_SuggestionTestCase):
    def test_emit_suggestion_list_card_skips_when_lark_not_primary(self) -> None:
        with patch.dict(
            os.environ, {"AGENTFLOW_LARK_APP_PRIMARY": "false"}, clear=False
        ), patch("agentflow.shared.agent_bridge.emit_agent_event") as emit_mock:
            review_triggers._emit_lark_suggestion_list_card(
                suggestions=[{"id": "s1", "title": "T", "profile_id": "p1"}]
            )
        emit_mock.assert_not_called()

    def test_emit_suggestion_list_card_emits_review_event(self) -> None:
        with patch.dict(
            os.environ, {"AGENTFLOW_LARK_APP_PRIMARY": "true"}, clear=False
        ), patch("agentflow.shared.agent_bridge.emit_agent_event") as emit_mock:
            review_triggers._emit_lark_suggestion_list_card(
                suggestions=[
                    {
                        "id": "s1",
                        "title": "Boost coverage",
                        "profile_id": "p1",
                        "stage": "self_check",
                        "source": "self_check",
                        "created_at": "2026-05-07T00:00:00Z",
                        "article_id": "art-1",
                    },
                    {
                        "id": "s2",
                        "title": "Tighten tone",
                        "profile_id": "p1",
                    },
                ]
            )
        emit_mock.assert_called_once()
        kwargs = emit_mock.call_args.kwargs
        self.assertEqual(kwargs["event_type"], "review.suggestion_list_card")
        self.assertEqual(kwargs["article_id"], "")
        payload = kwargs["payload"]
        self.assertEqual(payload["gate"], "S")
        self.assertEqual(payload["count"], 2)
        ids = [s["suggestion_id"] for s in payload["suggestions"]]
        self.assertEqual(ids, ["s1", "s2"])
        # Per-row buttons: 审阅 + 忽略, both carrying suggestion_id in payload.
        commands = [a["command"] for a in payload["suggestions"][0]["actions"]]
        self.assertIn("lark_suggestion_review", commands)
        self.assertIn("lark_suggestion_dismiss", commands)
        for action in payload["suggestions"][0]["actions"]:
            self.assertEqual(action["payload"]["suggestion_id"], "s1")


# ---------------------------------------------------------------------------
# _emit_lark_suggestion_review_card
# ---------------------------------------------------------------------------


class EmitSuggestionReviewCardTests(_SuggestionTestCase):
    def test_emit_suggestion_review_card_emits_review_event(self) -> None:
        with patch.dict(
            os.environ, {"AGENTFLOW_LARK_APP_PRIMARY": "true"}, clear=False
        ), patch("agentflow.shared.agent_bridge.emit_agent_event") as emit_mock:
            review_triggers._emit_lark_suggestion_review_card(
                suggestion={
                    "id": "s42",
                    "title": "Refine angle",
                    "summary": "Move from 'why' to 'how'.",
                    "profile_id": "p1",
                    "stage": "self_check",
                    "risk_level": "low",
                    "proposed_patch": {"voice": "punchier", "tags": ["x"]},
                    "article_id": "art-9",
                }
            )
        emit_mock.assert_called_once()
        kwargs = emit_mock.call_args.kwargs
        self.assertEqual(kwargs["event_type"], "review.suggestion_review_card")
        self.assertEqual(kwargs["article_id"], "art-9")
        payload = kwargs["payload"]
        self.assertEqual(payload["gate"], "S")
        self.assertEqual(payload["suggestion_id"], "s42")
        self.assertEqual(sorted(payload["changed_keys"]), ["tags", "voice"])
        commands = [a["command"] for a in payload["actions"]]
        self.assertIn("lark_suggestion_apply", commands)
        self.assertIn("lark_suggestion_dismiss", commands)
        self.assertIn("lark_suggestion_list", commands)


# ---------------------------------------------------------------------------
# Per-handler behaviour
# ---------------------------------------------------------------------------


class HandleSuggestionReviewTests(_SuggestionTestCase):
    def test_handle_suggestion_review_returns_review_card(self) -> None:
        _write_lark_operator(self.home, _OPERATOR["open_id"], ["*"])
        fake_suggestion = {
            "id": "s1",
            "title": "Refine",
            "profile_id": "p1",
            "stage": "self_check",
            "summary": "Body text.",
        }
        with patch(
            "agentflow.shared.topic_profile_lifecycle.review_suggestion",
            return_value={
                "suggestion": fake_suggestion,
                "current_profile": {},
                "preview_profile": {},
                "missing_fields": [],
            },
        ) as mock_review, patch.object(
            review_triggers, "_emit_lark_suggestion_review_card"
        ) as mock_emit:
            res = lark_callback.handle_event(
                event_kind="card_action",
                article_id=None,
                action="suggestion_review",
                payload={"suggestion_id": "s1"},
                operator=_OPERATOR,
            )
        mock_review.assert_called_once_with("s1")
        mock_emit.assert_called_once()
        self.assertTrue(res["ack"])
        self.assertIn("suggestion_review_emitted", res["side_effects"])
        self.assertIsNotNone(res["reply_card"])


class HandleSuggestionApplyTests(_SuggestionTestCase):
    def test_handle_suggestion_apply_returns_success_card(self) -> None:
        _write_lark_operator(self.home, _OPERATOR["open_id"], ["*"])
        with patch(
            "agentflow.shared.topic_profile_lifecycle.apply_suggestion",
            return_value={
                "suggestion": {"id": "s1", "profile_id": "p1", "status": "applied"},
                "applied": {"voice": "punchier"},
            },
        ) as mock_apply:
            res = lark_callback.handle_event(
                event_kind="card_action",
                article_id=None,
                action="suggestion_apply",
                payload={"suggestion_id": "s1"},
                operator=_OPERATOR,
            )
        mock_apply.assert_called_once_with("s1")
        self.assertTrue(res["ack"])
        self.assertIn("suggestion_applied", res["side_effects"])
        card = res["reply_card"]
        self.assertIsNotNone(card)
        self.assertEqual(card["header"]["template"], "green")

    def test_handle_suggestion_apply_unauthorized_open_id_returns_deny_card(self) -> None:
        # No lark_operators entry → fail-closed deny.
        with patch(
            "agentflow.shared.topic_profile_lifecycle.apply_suggestion"
        ) as mock_apply:
            res = lark_callback.handle_event(
                event_kind="card_action",
                article_id=None,
                action="suggestion_apply",
                payload={"suggestion_id": "s1"},
                operator=_OPERATOR,
            )
        mock_apply.assert_not_called()
        self.assertTrue(res["ack"])
        self.assertIn("not_authorized", res["side_effects"])
        card = res["reply_card"]
        self.assertIsNotNone(card)
        self.assertEqual(card["header"]["template"], "red")


class HandleSuggestionDismissTests(_SuggestionTestCase):
    def test_handle_suggestion_dismiss_returns_success_card(self) -> None:
        _write_lark_operator(self.home, _OPERATOR["open_id"], ["*"])
        with patch(
            "agentflow.shared.topic_profile_lifecycle.update_suggestion_status",
            return_value={"id": "s1", "status": "dismissed"},
        ) as mock_dismiss:
            res = lark_callback.handle_event(
                event_kind="card_action",
                article_id=None,
                action="suggestion_dismiss",
                payload={"suggestion_id": "s1"},
                operator=_OPERATOR,
            )
        mock_dismiss.assert_called_once_with("s1", "dismissed")
        self.assertTrue(res["ack"])
        self.assertIn("suggestion_dismissed", res["side_effects"])
        card = res["reply_card"]
        self.assertIsNotNone(card)
        self.assertEqual(card["header"]["template"], "grey")


# ---------------------------------------------------------------------------
# web.py registration
# ---------------------------------------------------------------------------


class WebLarkCommandsTests(unittest.TestCase):
    def test_web_lark_commands_registered(self) -> None:
        commands = review_web._COMMAND_SPECS
        for name in (
            "lark_suggestion_list",
            "lark_suggestion_review",
            "lark_suggestion_apply",
            "lark_suggestion_dismiss",
        ):
            self.assertIn(name, commands, f"missing command: {name}")
            spec = commands[name]
            self.assertTrue(spec["in_process"])
        # Apply mutates the user profile → must be flagged dangerous.
        self.assertTrue(commands["lark_suggestion_apply"]["dangerous"])
        # The other three are not dangerous.
        for name in (
            "lark_suggestion_list",
            "lark_suggestion_review",
            "lark_suggestion_dismiss",
        ):
            self.assertFalse(commands[name]["dangerous"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
