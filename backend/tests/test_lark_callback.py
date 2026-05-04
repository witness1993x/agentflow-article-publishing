"""Tests for ``agentflow.agent_review.lark_callback`` (Phase 1)."""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from agentflow.agent_review import lark_callback
from agentflow.agent_review import state as review_state
from agentflow.agent_review import triggers as review_triggers
from agentflow.agent_review.state import StateError
from agentflow.shared import bootstrap, memory


class _AgentflowHomeTestCase(unittest.TestCase):
    """Minimal AGENTFLOW_HOME tmpdir setUp mirroring test_v02_workflows.py."""

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


_OPERATOR = {"open_id": "ou_xxx_test", "name": "Tester"}


class UnknownActionTests(_AgentflowHomeTestCase):
    def test_unknown_action_returns_ack_with_side_effect(self) -> None:
        with patch.object(lark_callback.review_state, "transition") as mock_trans:
            res = lark_callback.handle_event(
                event_kind="card_action",
                article_id="art-1",
                action="unknown_thing",
                payload={},
                operator=_OPERATOR,
            )
        self.assertTrue(res["ack"])
        self.assertIn("unknown_action", res["side_effects"])
        self.assertIsNone(res["reply_card"])
        mock_trans.assert_not_called()


class ApproveBTests(_AgentflowHomeTestCase):
    def test_approve_b_transitions_state(self) -> None:
        with patch.object(lark_callback.review_state, "transition") as mock_trans, \
                patch.object(lark_callback, "append_memory_event") as mock_event:
            res = lark_callback.handle_event(
                event_kind="card_action",
                article_id="art-42",
                action="approve_b",
                payload={},
                operator=_OPERATOR,
            )
        self.assertTrue(res["ack"])
        self.assertIn("approve_b", res["side_effects"])
        self.assertIsNotNone(res["reply_card"])

        mock_trans.assert_called_once()
        _, kwargs = mock_trans.call_args
        # transition takes positional article_id + keyword-only kwargs
        args, _ = mock_trans.call_args
        self.assertEqual(args[0], "art-42")
        self.assertEqual(kwargs["gate"], "B")
        self.assertEqual(kwargs["to_state"], review_state.STATE_DRAFT_APPROVED)
        self.assertEqual(kwargs["actor"], "lark:ou_xxx_test")
        self.assertEqual(kwargs["decision"], "approve_via_lark")

        # Telemetry event recorded.
        mock_event.assert_called()
        types_called = [c.args[0] for c in mock_event.call_args_list]
        self.assertIn("lark_callback", types_called)

    def test_approve_b_idempotent_on_already_approved(self) -> None:
        with patch.object(
            lark_callback.review_state,
            "transition",
            side_effect=StateError("already approved"),
        ):
            res = lark_callback.handle_event(
                event_kind="card_action",
                article_id="art-99",
                action="approve_b",
                payload={},
                operator=_OPERATOR,
            )
        self.assertTrue(res["ack"])
        self.assertEqual(res["side_effects"], ["already_handled"])


class RejectBTests(_AgentflowHomeTestCase):
    def test_reject_b_transitions_to_drafting(self) -> None:
        with patch.object(lark_callback.review_state, "transition") as mock_trans:
            res = lark_callback.handle_event(
                event_kind="card_action",
                article_id="art-3",
                action="reject_b",
                payload={},
                operator=_OPERATOR,
            )
        self.assertTrue(res["ack"])
        mock_trans.assert_called_once()
        _, kwargs = mock_trans.call_args
        self.assertEqual(kwargs["to_state"], review_state.STATE_DRAFTING)
        self.assertEqual(kwargs["decision"], "reject_via_lark")


class RefillTests(_AgentflowHomeTestCase):
    def test_refill_returns_reply_card_no_state_mutation(self) -> None:
        with patch.object(lark_callback.review_state, "transition") as mock_trans:
            res = lark_callback.handle_event(
                event_kind="card_action",
                article_id="art-7",
                action="refill",
                payload={},
                operator=_OPERATOR,
            )
        self.assertTrue(res["ack"])
        self.assertIsNotNone(res["reply_card"])
        self.assertIn("Telegram", res["reply_card"]["elements"][0]["text"]["content"])
        mock_trans.assert_not_called()
        self.assertIn("refill_deferred_to_tg", res["side_effects"])


class TakeoverTests(_AgentflowHomeTestCase):
    def test_takeover_calls_post_locked_takeover(self) -> None:
        with patch.object(
            review_triggers,
            "post_locked_takeover",
            return_value={"article_id": "art-2", "tg_message_id": 123},
        ) as mock_trigger:
            res = lark_callback.handle_event(
                event_kind="card_action",
                article_id="art-2",
                action="takeover",
                payload={},
                operator=_OPERATOR,
            )
        self.assertTrue(res["ack"])
        mock_trigger.assert_called_once_with("art-2")
        self.assertIn("takeover_triggered", res["side_effects"])

    def test_takeover_skipped_when_trigger_returns_none(self) -> None:
        with patch.object(review_triggers, "post_locked_takeover", return_value=None):
            res = lark_callback.handle_event(
                event_kind="card_action",
                article_id="art-2",
                action="takeover",
                payload={},
                operator=_OPERATOR,
            )
        self.assertTrue(res["ack"])
        self.assertIn("takeover_skipped", res["side_effects"])


class ViewAuditTests(_AgentflowHomeTestCase):
    def test_view_audit_loads_memory_events(self) -> None:
        # Seed an audit event.
        memory.append_memory_event(
            "d2_structure_audit",
            article_id="art-audit",
            payload={"verdict": "pass", "summary": "skeleton looks good"},
        )
        # And an unrelated event we should NOT see.
        memory.append_memory_event(
            "some_other_event",
            article_id="art-audit",
            payload={"verdict": "ignored"},
        )

        res = lark_callback.handle_event(
            event_kind="card_action",
            article_id="art-audit",
            action="view_audit",
            payload={},
            operator=_OPERATOR,
        )
        self.assertTrue(res["ack"])
        self.assertIsNotNone(res["reply_card"])
        body = res["reply_card"]["elements"][0]["text"]["content"]
        self.assertIn("pass", body)
        self.assertIn("skeleton looks good", body)
        self.assertNotIn("ignored", body)

    def test_view_audit_empty_returns_card(self) -> None:
        res = lark_callback.handle_event(
            event_kind="card_action",
            article_id="art-empty",
            action="view_audit",
            payload={},
            operator=_OPERATOR,
        )
        self.assertTrue(res["ack"])
        body = res["reply_card"]["elements"][0]["text"]["content"]
        self.assertIn("暂无", body)


class ViewMetaTests(_AgentflowHomeTestCase):
    def test_view_meta_handles_missing_metadata(self) -> None:
        res = lark_callback.handle_event(
            event_kind="card_action",
            article_id="does-not-exist",
            action="view_meta",
            payload={},
            operator=_OPERATOR,
        )
        self.assertTrue(res["ack"])
        body = res["reply_card"]["elements"][0]["text"]["content"]
        self.assertIn("未找到", body)

    def test_view_meta_renders_existing_metadata(self) -> None:
        article_id = "art-meta"
        draft_dir = bootstrap.agentflow_home() / "drafts" / article_id
        draft_dir.mkdir(parents=True, exist_ok=True)
        (draft_dir / "metadata.json").write_text(
            json.dumps(
                {
                    "title": "Hello Lark",
                    "gate_history": [
                        {
                            "gate": "A",
                            "from_state": "topic_pool",
                            "to_state": "topic_approved",
                            "actor": "tg:1",
                            "decision": "approve",
                        },
                        {
                            "gate": "B",
                            "from_state": "draft_pending_review",
                            "to_state": "drafting",
                            "actor": "tg:1",
                            "decision": "rewrite_round",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        res = lark_callback.handle_event(
            event_kind="card_action",
            article_id=article_id,
            action="view_meta",
            payload={},
            operator=_OPERATOR,
        )
        self.assertTrue(res["ack"])
        body = res["reply_card"]["elements"][0]["text"]["content"]
        self.assertIn("Hello Lark", body)
        self.assertIn("drafting", body)
        self.assertIn("Rewrite rounds", body)


class UrlVerifyTests(_AgentflowHomeTestCase):
    def test_url_verify_returns_challenge(self) -> None:
        res = lark_callback.handle_event(
            event_kind="url_verify",
            article_id=None,
            action=None,
            payload={"challenge": "abc123"},
            operator={},
        )
        self.assertTrue(res["ack"])
        self.assertEqual(res["reply_text"], "abc123")
        self.assertIn("url_verify", res["side_effects"])


class MissingArticleIdTests(_AgentflowHomeTestCase):
    def test_card_action_without_article_id_short_circuits(self) -> None:
        with patch.object(lark_callback.review_state, "transition") as mock_trans:
            res = lark_callback.handle_event(
                event_kind="card_action",
                article_id=None,
                action="approve_b",
                payload={},
                operator=_OPERATOR,
            )
        self.assertTrue(res["ack"])
        self.assertIn("missing_article_id", res["side_effects"])
        mock_trans.assert_not_called()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
