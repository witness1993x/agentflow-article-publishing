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
    def test_refill_transitions_and_spawns_auto_pick_fill(self) -> None:
        with patch.object(lark_callback.review_state, "transition") as mock_trans, \
                patch.object(lark_callback, "_spawn_async", return_value=True) as mock_spawn:
            res = lark_callback.handle_event(
                event_kind="card_action",
                article_id="art-7",
                action="refill",
                payload={},
                operator=_OPERATOR,
            )
        self.assertTrue(res["ack"])
        self.assertIsNotNone(res["reply_card"])
        self.assertIn("refill_spawned", res["side_effects"])
        mock_trans.assert_called_once()
        args, kwargs = mock_trans.call_args
        self.assertEqual(args[0], "art-7")
        self.assertEqual(kwargs["gate"], "B")
        self.assertEqual(kwargs["to_state"], review_state.STATE_DRAFTING)
        self.assertEqual(kwargs["actor"], "lark:ou_xxx_test")
        self.assertEqual(kwargs["decision"], "refill_via_lark")

        argv = mock_spawn.call_args.args[0]
        self.assertIn("fill", argv)
        self.assertIn("art-7", argv)
        self.assertIn("--skeleton-only", argv)
        self.assertIn("--auto-pick", argv)
        self.assertIn("--json", argv)
        self.assertEqual(mock_spawn.call_args.kwargs["article_id"], "art-7")
        self.assertEqual(mock_spawn.call_args.kwargs["action"], "refill")

    def test_refill_returns_already_handled_on_state_error(self) -> None:
        with patch.object(
            lark_callback.review_state,
            "transition",
            side_effect=StateError("already rejected"),
        ), patch.object(lark_callback, "_spawn_async") as mock_spawn:
            res = lark_callback.handle_event(
                event_kind="card_action",
                article_id="art-7",
                action="refill",
                payload={},
                operator=_OPERATOR,
            )
        self.assertTrue(res["ack"])
        self.assertEqual(res["side_effects"], ["already_handled"])
        self.assertIsNotNone(res["reply_card"])
        mock_spawn.assert_not_called()


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


def _make_meta(home: Path, article_id: str, payload: dict | None = None) -> Path:
    """Create a metadata.json under <home>/drafts/<id>/ with the given payload."""
    p = home / "drafts" / article_id
    p.mkdir(parents=True, exist_ok=True)
    f = p / "metadata.json"
    f.write_text(json.dumps(payload or {}, ensure_ascii=False), encoding="utf-8")
    return f


# ---------------------------------------------------------------------------
# v1.1.1 — Gate A handlers
# ---------------------------------------------------------------------------


class GateAWriteTests(_AgentflowHomeTestCase):
    def test_spawn_invoked_with_correct_argv(self) -> None:
        with patch.object(lark_callback, "_spawn_async", return_value=True) as mock_spawn:
            res = lark_callback.handle_event(
                event_kind="card_action",
                article_id="hs_x",
                action="gate_a_write",
                payload={"angle_index": 2, "target_series": "B"},
                operator=_OPERATOR,
            )
        self.assertTrue(res["ack"])
        self.assertIn("gate_a_write_spawned", res["side_effects"])
        argv = mock_spawn.call_args.args[0]
        self.assertIn("write", argv)
        self.assertIn("hs_x", argv)
        self.assertIn("--angle-index", argv)
        self.assertIn("2", argv)

    def test_spawn_failure_returns_red_card(self) -> None:
        with patch.object(lark_callback, "_spawn_async", return_value=False):
            res = lark_callback.handle_event(
                event_kind="card_action",
                article_id="hs_x",
                action="gate_a_write",
                payload={},
                operator=_OPERATOR,
            )
        self.assertIn("spawn_failed", res["side_effects"])
        self.assertEqual(res["reply_card"]["header"]["template"], "red")


class GateARejectAllTests(_AgentflowHomeTestCase):
    def test_returns_red_card_no_spawn(self) -> None:
        with patch.object(lark_callback, "_spawn_async") as mock_spawn:
            res = lark_callback.handle_event(
                event_kind="card_action",
                article_id="hs_x",
                action="gate_a_reject_all",
                payload={},
                operator=_OPERATOR,
            )
        self.assertIn("gate_a_reject_all", res["side_effects"])
        self.assertEqual(res["reply_card"]["header"]["template"], "red")
        mock_spawn.assert_not_called()


class GateAExpandTests(_AgentflowHomeTestCase):
    def test_expand_finds_hotspot_and_renders_card(self) -> None:
        hpath = self.home / "hotspots"
        hpath.mkdir(parents=True, exist_ok=True)
        (hpath / "2026-05-06.json").write_text(
            json.dumps(
                {
                    "hotspots": [
                        {
                            "id": "hs_target",
                            "topic_one_liner": "RLUSD 跨链",
                            "mainstream_views": ["v1", "v2"],
                            "overlooked_angles": ["a1"],
                        }
                    ]
                }
            )
        )
        res = lark_callback.handle_event(
            event_kind="card_action",
            article_id="hs_target",
            action="gate_a_expand",
            payload={},
            operator=_OPERATOR,
        )
        self.assertIn("gate_a_expand", res["side_effects"])
        body = res["reply_card"]["elements"][0]["text"]["content"]
        self.assertIn("RLUSD", body)
        self.assertIn("v1", body)

    def test_expand_returns_grey_when_not_found(self) -> None:
        res = lark_callback.handle_event(
            event_kind="card_action",
            article_id="missing",
            action="gate_a_expand",
            payload={},
            operator=_OPERATOR,
        )
        self.assertIn("hotspot_not_found", res["side_effects"])
        self.assertEqual(res["reply_card"]["header"]["template"], "grey")


# ---------------------------------------------------------------------------
# v1.1.1 — Gate B remaining
# ---------------------------------------------------------------------------


class GateBRewriteTests(_AgentflowHomeTestCase):
    def test_rewrite_transitions_state_then_spawns(self) -> None:
        _make_meta(self.home, "art_b", {
            "gate_history": [{"to_state": "draft_pending_review", "gate": "B"}],
        })
        with patch.object(lark_callback.review_state, "transition") as mock_trans, \
             patch.object(lark_callback, "_spawn_async", return_value=True) as mock_spawn:
            res = lark_callback.handle_event(
                event_kind="card_action",
                article_id="art_b",
                action="gate_b_rewrite",
                payload={},
                operator=_OPERATOR,
            )
        self.assertIn("gate_b_rewrite_spawned", res["side_effects"])
        mock_trans.assert_called_once()
        self.assertEqual(mock_trans.call_args.kwargs["to_state"], "drafting")
        mock_spawn.assert_called_once()

    def test_rewrite_already_handled_returns_grey(self) -> None:
        from agentflow.agent_review.state import StateError as _SE
        with patch.object(
            lark_callback.review_state, "transition", side_effect=_SE("nope")
        ), patch.object(lark_callback, "_spawn_async") as mock_spawn:
            res = lark_callback.handle_event(
                event_kind="card_action",
                article_id="art_b",
                action="gate_b_rewrite",
                payload={},
                operator=_OPERATOR,
            )
        self.assertIn("already_handled", res["side_effects"])
        mock_spawn.assert_not_called()


class GateBEditTests(_AgentflowHomeTestCase):
    def test_edit_logs_pending_event(self) -> None:
        res = lark_callback.handle_event(
            event_kind="card_action",
            article_id="art_e",
            action="gate_b_edit",
            payload={"section_index": 1, "paragraph_index": 0},
            operator=_OPERATOR,
        )
        self.assertIn("gate_b_edit_pending", res["side_effects"])
        body = res["reply_card"]["elements"][0]["text"]["content"]
        self.assertIn("section=1", body)

    def test_edit_with_inline_text_spawns_edit_command(self) -> None:
        with patch.object(lark_callback, "_spawn_async", return_value=True) as mock_spawn:
            res = lark_callback.handle_event(
                event_kind="card_action",
                article_id="art_e",
                action="gate_b_edit",
                payload={
                    "section_index": 1,
                    "paragraph_index": 0,
                    "comment": "第二段补一个数据例子",
                },
                operator=_OPERATOR,
            )
        self.assertIn("gate_b_edit_spawned", res["side_effects"])
        argv = mock_spawn.call_args.args[0]
        self.assertIn("edit", argv)
        self.assertIn("art_e", argv)
        self.assertIn("--section", argv)
        self.assertIn("1", argv)
        self.assertIn("--paragraph", argv)
        self.assertIn("0", argv)
        self.assertIn("--command", argv)
        self.assertIn("第二段补一个数据例子", argv)
        self.assertIn("--post-review", argv)

    def test_edit_with_inline_text_requires_target(self) -> None:
        with patch.object(lark_callback, "_spawn_async") as mock_spawn:
            res = lark_callback.handle_event(
                event_kind="card_action",
                article_id="art_e",
                action="gate_b_edit",
                payload={"comment": "整体改得更锋利"},
                operator=_OPERATOR,
            )
        self.assertIn("gate_b_edit_missing_target", res["side_effects"])
        self.assertEqual(res["reply_card"]["header"]["template"], "orange")
        mock_spawn.assert_not_called()

    def test_apply_pending_edit_uses_latest_gate_b_slot(self) -> None:
        memory.append_memory_event(
            "lark_edit_pending",
            article_id="art_e",
            payload={
                "operator_open_id": _OPERATOR["open_id"],
                "section_index": 2,
                "paragraph_index": 1,
            },
        )
        with patch.object(lark_callback, "_spawn_async", return_value=True) as mock_spawn:
            res = lark_callback.handle_event(
                event_kind="card_action",
                article_id="art_e",
                action="apply_pending_edit",
                payload={"text": "这里加一个反例"},
                operator=_OPERATOR,
            )
        self.assertIn("apply_pending_edit_spawned", res["side_effects"])
        argv = mock_spawn.call_args.args[0]
        self.assertIn("edit", argv)
        self.assertIn("--section", argv)
        self.assertIn("2", argv)
        self.assertIn("--paragraph", argv)
        self.assertIn("1", argv)
        self.assertIn("--post-review", argv)
        self.assertIn("这里加一个反例", argv)

    def test_apply_pending_edit_does_not_reuse_consumed_slot(self) -> None:
        memory.append_memory_event(
            "lark_edit_pending",
            article_id="art_e",
            payload={
                "operator_open_id": _OPERATOR["open_id"],
                "section_index": 2,
            },
        )
        with patch.object(lark_callback, "_spawn_async", return_value=True):
            first = lark_callback.handle_event(
                event_kind="card_action",
                article_id="art_e",
                action="apply_pending_edit",
                payload={"text": "补一个例子"},
                operator=_OPERATOR,
            )
        self.assertIn("apply_pending_edit_spawned", first["side_effects"])

        with patch.object(lark_callback, "_spawn_async") as mock_spawn:
            second = lark_callback.handle_event(
                event_kind="card_action",
                article_id="art_e",
                action="apply_pending_edit",
                payload={"text": "再补一次"},
                operator=_OPERATOR,
            )
        self.assertIn("pending_edit_not_found", second["side_effects"])
        mock_spawn.assert_not_called()


class GateBDiffTests(_AgentflowHomeTestCase):
    def test_diff_renders_audit_event(self) -> None:
        memory.append_memory_event(
            "d2_structure_audit",
            article_id="art_d",
            payload={
                "verdict": "patch",
                "score": 0.62,
                "dim_scores": {"cohesion": 0.5},
                "issues": ["[Section 1] missing anchor"],
            },
        )
        res = lark_callback.handle_event(
            event_kind="card_action",
            article_id="art_d",
            action="gate_b_diff",
            payload={},
            operator=_OPERATOR,
        )
        body = res["reply_card"]["elements"][0]["text"]["content"]
        self.assertIn("patch", body)
        self.assertIn("missing anchor", body)


# ---------------------------------------------------------------------------
# v1.1.1 — Gate C
# ---------------------------------------------------------------------------


class GateCTests(_AgentflowHomeTestCase):
    def test_approve_transitions_to_image_approved(self) -> None:
        with patch.object(lark_callback.review_state, "transition") as mock_trans:
            res = lark_callback.handle_event(
                event_kind="card_action",
                article_id="art_c",
                action="gate_c_approve",
                payload={},
                operator=_OPERATOR,
            )
        self.assertIn("gate_c_approve", res["side_effects"])
        self.assertEqual(mock_trans.call_args.kwargs["to_state"], "image_approved")

    def test_skip_transitions_to_image_skipped(self) -> None:
        with patch.object(lark_callback.review_state, "transition") as mock_trans:
            res = lark_callback.handle_event(
                event_kind="card_action",
                article_id="art_c",
                action="gate_c_skip",
                payload={},
                operator=_OPERATOR,
            )
        self.assertIn("gate_c_skip", res["side_effects"])
        self.assertEqual(mock_trans.call_args.kwargs["to_state"], "image_skipped")

    def test_regen_spawns_image_gate(self) -> None:
        with patch.object(lark_callback, "_spawn_async", return_value=True) as mock_spawn:
            res = lark_callback.handle_event(
                event_kind="card_action",
                article_id="art_c",
                action="gate_c_regen",
                payload={"mode": "logo_centric"},
                operator=_OPERATOR,
            )
        argv = mock_spawn.call_args.args[0]
        self.assertIn("image-gate", argv)
        self.assertIn("--mode", argv)
        self.assertIn("logo_centric", argv)
        self.assertIn("gate_c_regen_spawned", res["side_effects"])

    def test_regen_passes_inline_prompt_to_image_gate(self) -> None:
        with patch.object(lark_callback, "_spawn_async", return_value=True) as mock_spawn:
            res = lark_callback.handle_event(
                event_kind="card_action",
                article_id="art_c",
                action="gate_c_regen",
                payload={
                    "mode": "cover-only",
                    "prompt": "更像链上数据雷达，不要抽象机器人",
                },
                operator=_OPERATOR,
            )
        argv = mock_spawn.call_args.args[0]
        self.assertIn("--cover-description", argv)
        self.assertIn("更像链上数据雷达，不要抽象机器人", argv)
        self.assertIn("gate_c_regen_spawned", res["side_effects"])

    def test_full_renders_image_placeholder_list(self) -> None:
        _make_meta(self.home, "art_c", {
            "image_placeholders": [
                {"description": "封面 hero", "resolved_path": "/tmp/a.png"},
                {"description": "图 2", "resolved_path": None},
            ]
        })
        res = lark_callback.handle_event(
            event_kind="card_action",
            article_id="art_c",
            action="gate_c_full",
            payload={},
            operator=_OPERATOR,
        )
        body = res["reply_card"]["elements"][0]["text"]["content"]
        self.assertIn("封面 hero", body)

    def test_image_picker_cover_only_spawns_image_gate(self) -> None:
        with patch.object(lark_callback, "_spawn_async", return_value=True) as mock_spawn:
            res = lark_callback.handle_event(
                event_kind="card_action",
                article_id="art_c",
                action="image_gate_pick",
                payload={"mode": "cover-only"},
                operator=_OPERATOR,
            )
        argv = mock_spawn.call_args.args[0]
        self.assertIn("image-gate", argv)
        self.assertIn("--mode", argv)
        self.assertIn("cover-only", argv)
        self.assertIn("image_gate_pick_spawned", res["side_effects"])

    def test_image_picker_skip_posts_gate_d(self) -> None:
        with patch.object(lark_callback.review_state, "transition") as mock_trans, \
             patch.object(lark_callback.review_triggers, "post_gate_d") as post_d:
            res = lark_callback.handle_event(
                event_kind="card_action",
                article_id="art_c",
                action="image_gate_pick",
                payload={"mode": "none"},
                operator=_OPERATOR,
            )
        self.assertIn("image_gate_skipped", res["side_effects"])
        self.assertEqual(mock_trans.call_args.kwargs["to_state"], "image_skipped")
        post_d.assert_called_once_with("art_c")


# ---------------------------------------------------------------------------
# v1.1.1 — Gate D
# ---------------------------------------------------------------------------


class GateDTests(_AgentflowHomeTestCase):
    def test_toggle_adds_then_removes_platform(self) -> None:
        _make_meta(self.home, "art_d", {})
        # Toggle on
        res1 = lark_callback.handle_event(
            event_kind="card_action",
            article_id="art_d",
            action="gate_d_toggle",
            payload={"platform": "medium"},
            operator=_OPERATOR,
        )
        self.assertIn("gate_d_toggle_on", res1["side_effects"])
        # Toggle off
        res2 = lark_callback.handle_event(
            event_kind="card_action",
            article_id="art_d",
            action="gate_d_toggle",
            payload={"platform": "medium"},
            operator=_OPERATOR,
        )
        self.assertIn("gate_d_toggle_off", res2["side_effects"])
        meta = json.loads((self.home / "drafts" / "art_d" / "metadata.json").read_text())
        self.assertEqual(meta["gate_d_selection"], [])

    def test_select_all_overwrites_selection(self) -> None:
        _make_meta(self.home, "art_d", {"gate_d_selection": ["medium"]})
        res = lark_callback.handle_event(
            event_kind="card_action",
            article_id="art_d",
            action="gate_d_select_all",
            payload={"platforms": ["medium", "ghost"]},
            operator=_OPERATOR,
        )
        self.assertIn("gate_d_select_all", res["side_effects"])
        meta = json.loads((self.home / "drafts" / "art_d" / "metadata.json").read_text())
        self.assertEqual(meta["gate_d_selection"], ["medium", "ghost"])

    def test_confirm_with_empty_selection_does_not_spawn(self) -> None:
        _make_meta(self.home, "art_d", {"gate_d_selection": []})
        with patch.object(lark_callback, "_spawn_async") as mock_spawn:
            res = lark_callback.handle_event(
                event_kind="card_action",
                article_id="art_d",
                action="gate_d_confirm",
                payload={},
                operator=_OPERATOR,
            )
        self.assertIn("empty_selection", res["side_effects"])
        mock_spawn.assert_not_called()

    def test_confirm_with_selection_spawns_dispatch_chain(self) -> None:
        _make_meta(self.home, "art_d", {"gate_d_selection": ["medium", "ghost"]})
        with patch.object(lark_callback, "_spawn_publish_dispatch", return_value=True) as mock_spawn:
            res = lark_callback.handle_event(
                event_kind="card_action",
                article_id="art_d",
                action="gate_d_confirm",
                payload={},
                operator=_OPERATOR,
            )
        self.assertIn("gate_d_dispatch_spawned", res["side_effects"])
        mock_spawn.assert_called_once_with(
            "art_d", ["medium", "ghost"], operator=_OPERATOR
        )

    def test_save_default_writes_preferences_file(self) -> None:
        _make_meta(self.home, "art_d", {"gate_d_selection": ["medium", "ghost"]})
        res = lark_callback.handle_event(
            event_kind="card_action",
            article_id="art_d",
            action="gate_d_save_default",
            payload={},
            operator=_OPERATOR,
        )
        self.assertIn("gate_d_save_default", res["side_effects"])
        prefs = json.loads((self.home / "preferences.json").read_text())
        self.assertEqual(prefs["gate_d"]["default_platforms"], ["medium", "ghost"])


# ---------------------------------------------------------------------------
# v1.1.1 — Locked Takeover (L)
# ---------------------------------------------------------------------------


class LockedTakeoverTests(_AgentflowHomeTestCase):
    def test_critique_renders_audit_event(self) -> None:
        memory.append_memory_event(
            "d2_structure_audit",
            article_id="art_l",
            payload={
                "verdict": "rewrite",
                "score": 0.3,
                "issues": ["all over the place"],
            },
        )
        res = lark_callback.handle_event(
            event_kind="card_action",
            article_id="art_l",
            action="locked_critique",
            payload={},
            operator=_OPERATOR,
        )
        body = res["reply_card"]["elements"][0]["text"]["content"]
        self.assertIn("rewrite", body)
        self.assertIn("all over the place", body)

    def test_apply_pending_locked_edit_parses_prefixed_section(self) -> None:
        memory.append_memory_event(
            "lark_locked_edit_pending",
            article_id="art_l",
            payload={"operator_open_id": _OPERATOR["open_id"]},
        )
        with patch.object(lark_callback, "_spawn_async", return_value=True) as mock_spawn:
            res = lark_callback.handle_event(
                event_kind="card_action",
                article_id="art_l",
                action="apply_pending_edit",
                payload={"text": "2 重写这一节，让论点更直接"},
                operator=_OPERATOR,
            )
        self.assertIn("apply_pending_edit_spawned", res["side_effects"])
        argv = mock_spawn.call_args.args[0]
        self.assertIn("--section", argv)
        self.assertIn("2", argv)
        self.assertIn("--command", argv)
        self.assertIn("重写这一节，让论点更直接", argv)

    def test_apply_pending_edit_without_slot_returns_grey_card(self) -> None:
        with patch.object(lark_callback, "_spawn_async") as mock_spawn:
            res = lark_callback.handle_event(
                event_kind="card_action",
                article_id="art_l",
                action="apply_pending_edit",
                payload={"text": "2 重写这一节"},
                operator=_OPERATOR,
            )
        self.assertIn("pending_edit_not_found", res["side_effects"])
        self.assertEqual(res["reply_card"]["header"]["template"], "grey")
        mock_spawn.assert_not_called()

    def test_give_up_transitions_to_draft_rejected(self) -> None:
        with patch.object(lark_callback.review_state, "transition") as mock_trans:
            res = lark_callback.handle_event(
                event_kind="card_action",
                article_id="art_l",
                action="locked_give_up",
                payload={},
                operator=_OPERATOR,
            )
        self.assertIn("locked_give_up", res["side_effects"])
        self.assertEqual(mock_trans.call_args.kwargs["to_state"], "draft_rejected")


# ---------------------------------------------------------------------------
# v1.1.1 — Defer
# ---------------------------------------------------------------------------


class DeferTests(_AgentflowHomeTestCase):
    def test_defer_returns_grey_card_no_state_mutation(self) -> None:
        with patch.object(lark_callback.review_state, "transition") as mock_trans:
            res = lark_callback.handle_event(
                event_kind="card_action",
                article_id="art_x",
                action="defer",
                payload={"gate": "B"},
                operator=_OPERATOR,
            )
        self.assertIn("deferred", res["side_effects"])
        self.assertEqual(res["reply_card"]["header"]["template"], "grey")
        mock_trans.assert_not_called()


# ---------------------------------------------------------------------------
# Free-text @-mention routing — v1.1.7 anti-hallucination guard.
#
# These tests pin the deterministic shape of lark_message replies so the
# Lark-side bot never has to fabricate a response. Every input must produce
# either a concrete handler dispatch or a structured "I don't understand"
# card — never silence.
# ---------------------------------------------------------------------------


class _FreeTextRoutingHelper(_AgentflowHomeTestCase):
    """Shared scaffolding for tests that need a draft_pending_review article."""

    def _create_article_in_state(self, aid: str, current_state: str) -> None:
        _make_meta(self.home, aid, {"title": aid, "gate_history": []})
        review_state.transition(
            aid, gate="A", to_state=review_state.STATE_TOPIC_APPROVED,
            actor="lark:test", decision="seed",
        )
        review_state.transition(
            aid, gate="A", to_state=review_state.STATE_DRAFTING,
            actor="lark:test", decision="seed",
        )
        if current_state == review_state.STATE_DRAFT_PENDING_REVIEW:
            review_state.transition(
                aid, gate="B", to_state=review_state.STATE_DRAFT_PENDING_REVIEW,
                actor="lark:test", decision="seed",
            )
        elif current_state == review_state.STATE_IMAGE_PENDING_REVIEW:
            review_state.transition(
                aid, gate="B", to_state=review_state.STATE_DRAFT_PENDING_REVIEW,
                actor="lark:test", decision="seed",
            )
            review_state.transition(
                aid, gate="B", to_state=review_state.STATE_DRAFT_APPROVED,
                actor="lark:test", decision="seed",
            )
            review_state.transition(
                aid, gate="C", to_state=review_state.STATE_IMAGE_PENDING_REVIEW,
                actor="lark:test", decision="seed",
            )


class FreeTextIntentClassifierTests(unittest.TestCase):
    """White-box tests on _classify_intent — keyword matrix lives here so a
    drift in the table is caught before the integration tests fire."""

    def test_chinese_approve_maps_to_approve_b(self) -> None:
        self.assertEqual(lark_callback._classify_intent("通过"), "approve_b")

    def test_english_approve_maps_to_approve_b(self) -> None:
        self.assertEqual(lark_callback._classify_intent("approve"), "approve_b")

    def test_reject_chinese(self) -> None:
        self.assertEqual(lark_callback._classify_intent("拒绝"), "reject_b")

    def test_rewrite(self) -> None:
        self.assertEqual(lark_callback._classify_intent("整篇重写"), "gate_b_rewrite")

    def test_advance_chinese(self) -> None:
        self.assertEqual(
            lark_callback._classify_intent("这篇推进到下个 gate"), "_advance"
        )

    def test_unknown_returns_none(self) -> None:
        self.assertIsNone(lark_callback._classify_intent("天气怎么样"))

    def test_normalize_strips_lark_at_user_placeholder(self) -> None:
        # Lark delivers @-mentions as @_user_N placeholders; the human-
        # readable name is supplied separately in `at_users`.
        self.assertEqual(lark_callback._normalize_text("@_user_1 通过"), "通过")
        self.assertEqual(lark_callback._normalize_text("  @bot 推进  "), "推进")

    def test_normalize_strips_inline_mentions_so_substrings_do_not_match(self) -> None:
        # Regression: the literal hallucination from the v1.1.7 screenshot
        # used to false-match ``audit`` inside ``@CSAuditContentPostBot``.
        text = "完成！Gate B 卡片已发到 TG @CSAuditContentPostBot"
        self.assertIsNone(lark_callback._classify_intent(
            lark_callback._normalize_text(text)
        ))

    def test_ascii_keyword_matches_only_on_word_boundary(self) -> None:
        # ``edit`` should match ``please edit`` but not ``editorial``.
        self.assertEqual(lark_callback._classify_intent("please edit it"), "gate_b_edit")
        self.assertIsNone(lark_callback._classify_intent("editorial board"))


class FreeTextRouterTests(_FreeTextRoutingHelper):

    def test_unknown_intent_returns_help_card_not_silence(self) -> None:
        """Anti-hallucination: any unparseable @-mention must produce a
        deterministic help card so the Lark bot doesn't fabricate a 'Gate B
        completed' reply."""
        res = lark_callback.handle_event(
            event_kind="message",
            article_id=None,
            action=None,
            payload={"text": "完成！Gate B 卡片已发到 TG"},
            operator=_OPERATOR,
        )
        self.assertTrue(res["ack"])
        self.assertIn("unknown_intent", res["side_effects"])
        self.assertIsNotNone(res["reply_card"])
        self.assertIn("没看懂", res["reply_card"]["elements"][0]["text"]["content"])

    def test_empty_message_returns_help_card(self) -> None:
        res = lark_callback.handle_event(
            event_kind="message", article_id=None, action=None,
            payload={"text": "   "}, operator=_OPERATOR,
        )
        self.assertIn("empty_message", res["side_effects"])

    def test_advance_with_no_active_article_returns_friendly_card(self) -> None:
        res = lark_callback.handle_event(
            event_kind="message", article_id=None, action=None,
            payload={"text": "推进到下个 gate"}, operator=_OPERATOR,
        )
        self.assertIn("no_active_article", res["side_effects"])
        self.assertIsNotNone(res["reply_card"])

    def test_advance_at_draft_pending_review_calls_approve_b(self) -> None:
        aid = "art_advance_b"
        self._create_article_in_state(aid, review_state.STATE_DRAFT_PENDING_REVIEW)
        res = lark_callback.handle_event(
            event_kind="message", article_id=None, action=None,
            payload={"text": "推进到下个 gate"}, operator=_OPERATOR,
        )
        self.assertIn("approve_b", res["side_effects"])

    def test_approve_intent_at_draft_pending_review_dispatches(self) -> None:
        aid = "art_approve_intent"
        self._create_article_in_state(aid, review_state.STATE_DRAFT_PENDING_REVIEW)
        res = lark_callback.handle_event(
            event_kind="message", article_id=None, action=None,
            payload={"text": "通过"}, operator=_OPERATOR,
        )
        self.assertIn("approve_b", res["side_effects"])

    def test_chat_id_propagates_into_telemetry(self) -> None:
        aid = "art_chatid"
        self._create_article_in_state(aid, review_state.STATE_DRAFT_PENDING_REVIEW)
        chat_id = "oc_lark_chat_42"
        lark_callback.handle_event(
            event_kind="message", article_id=None, action=None,
            payload={"text": "通过", "chat_id": chat_id},
            operator=_OPERATOR,
        )
        events = memory.read_memory_events(article_id=aid, event_type="lark_callback")
        chat_seen = any(
            (ev.get("payload") or {}).get("chat_id") == chat_id for ev in events
        )
        self.assertTrue(chat_seen, "chat_id must be threaded into telemetry payloads")

    def test_pending_edit_priority_over_intent_match(self) -> None:
        """Free text routes to apply_pending_edit when a pending slot exists,
        even if the text otherwise looks like an intent keyword."""
        aid = "art_pending_first"
        self._create_article_in_state(aid, review_state.STATE_DRAFT_PENDING_REVIEW)
        memory.append_memory_event(
            "lark_edit_pending",
            article_id=aid,
            payload={"operator_open_id": _OPERATOR["open_id"]},
        )
        with patch.object(
            lark_callback, "_handle_apply_pending_edit",
            wraps=lark_callback._handle_apply_pending_edit,
        ) as wrapped:
            lark_callback.handle_event(
                event_kind="message", article_id=None, action=None,
                payload={"text": "改一下第二段措辞"},
                operator=_OPERATOR,
            )
        wrapped.assert_called_once()


class FanOutClosureTests(_FreeTextRoutingHelper):
    """v1.1.8 closure regression: every Gate-state transition that has a
    natural follow-up card on the TG side must also fire that card on the
    Lark side. Otherwise the operator approves Gate B and gets stranded
    with no Gate C card waiting."""

    def test_approve_b_spawns_image_gate_picker(self) -> None:
        aid = "art_fan_b"
        self._create_article_in_state(aid, review_state.STATE_DRAFT_PENDING_REVIEW)
        with patch.object(
            lark_callback, "_spawn_next_gate_card",
        ) as mock_spawn:
            res = lark_callback.handle_event(
                event_kind="card_action", article_id=aid,
                action="approve_b", payload={}, operator=_OPERATOR,
            )
        mock_spawn.assert_called_once_with(aid, kind="image_picker")
        self.assertIn("image_picker_spawned", res["side_effects"])

    def test_gate_c_approve_spawns_gate_d(self) -> None:
        aid = "art_fan_c_approve"
        self._create_article_in_state(aid, review_state.STATE_IMAGE_PENDING_REVIEW)
        with patch.object(
            lark_callback, "_spawn_next_gate_card",
        ) as mock_spawn:
            res = lark_callback.handle_event(
                event_kind="card_action", article_id=aid,
                action="gate_c_approve", payload={}, operator=_OPERATOR,
            )
        mock_spawn.assert_called_once_with(aid, kind="gate_d")
        self.assertIn("gate_d_spawned", res["side_effects"])

    def test_gate_c_skip_spawns_gate_d(self) -> None:
        aid = "art_fan_c_skip"
        self._create_article_in_state(aid, review_state.STATE_IMAGE_PENDING_REVIEW)
        with patch.object(
            lark_callback, "_spawn_next_gate_card",
        ) as mock_spawn:
            res = lark_callback.handle_event(
                event_kind="card_action", article_id=aid,
                action="gate_c_skip", payload={}, operator=_OPERATOR,
            )
        mock_spawn.assert_called_once_with(aid, kind="gate_d")
        self.assertIn("gate_d_spawned", res["side_effects"])


class LarkAuthGateTests(_FreeTextRoutingHelper):
    """Per-action auth parity with TG's _ACTION_REQ."""

    def test_unauthorized_open_id_for_publish_action_is_denied(self) -> None:
        from agentflow.agent_review import auth as review_auth

        # Configure a closed v2 Lark allowlist (auth.json::lark_operators):
        # one reviewer with the ``review`` grant only — no ``publish``.
        # (post-L-4: legacy lark_auth.json + LARK_OPERATOR_OPEN_ID env are
        # no longer consulted by in-module handlers.)
        review_auth.lark_operator_add(
            "ou_reviewer", name="Reviewer", actions=["review"],
        )
        res = lark_callback.handle_event(
            event_kind="card_action",
            article_id="art_x",
            action="gate_d_confirm",  # requires publish
            payload={},
            operator={"open_id": "ou_reviewer", "name": "Reviewer"},
        )
        self.assertIn("not_authorized", res["side_effects"])
        self.assertEqual(res["reply_card"]["header"]["template"], "red")

    def test_operator_open_id_always_authorized(self) -> None:
        from agentflow.agent_review import auth as review_auth

        # Seed an explicit ``["*"]`` Lark operator entry so the v2 fail-closed
        # path approves the call. The operator-env override no longer applies
        # to in-module handlers post-L-4.
        review_auth.lark_operator_add(
            "ou_admin", name="Admin", actions=["*"],
        )
        with patch.object(
            lark_callback.review_state, "transition"
        ) as mock_trans:
            res = lark_callback.handle_event(
                event_kind="card_action",
                article_id="art_x",
                action="approve_b",
                payload={},
                operator={"open_id": "ou_admin", "name": "Admin"},
            )
        self.assertNotIn("not_authorized", res["side_effects"])
        mock_trans.assert_called_once()


import os  # noqa: E402  — used by LarkAuthGateTests env patches


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
