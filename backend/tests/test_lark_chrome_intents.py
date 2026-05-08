"""Tests for the 12 GAP-CHROME operator slash-command parity intents.

Covers (per intent):
* Happy-path: free-text @-bot input → correct chrome handler invoked +
  ``review.*_card`` event emitted.
* False-positive guard: a similar but *not* whole-text-matching phrase
  must NOT trip the chrome dispatch.

Plus cross-cutting:
* All chrome handlers are gated by ``_authorize_or_deny_v2`` (fail-closed).
* The existing ``推进 / advance`` intent is not shadowed by chrome.
* Mutating chrome handlers (skip / publish_mark / cancel) write a
  ``gate_history`` entry.
* All 12 ``lark_chrome_*`` commands appear in ``_LARK_COMMANDS``.

Plan ref: docs/BLOGFLOW_TG_TO_LARK_PARITY.md §3.4 GAP-CHROME.
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
from agentflow.agent_review import state as review_state
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
        json.dumps({
            "lark_operators": [
                {"open_id": open_id, "name": "Tester", "actions": actions},
            ],
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _seed_article(home: Path, article_id: str, *, state: str, title: str = "T") -> None:
    """Write a minimal drafts/<id>/metadata.json with one gate_history entry."""
    art_dir = home / "drafts" / article_id
    art_dir.mkdir(parents=True, exist_ok=True)
    (art_dir / "metadata.json").write_text(
        json.dumps({
            "title": title,
            "gate_history": [{
                "gate": "X",
                "from_state": "drafting",
                "to_state": state,
                "actor": "test",
                "decision": "seed",
                "timestamp": "2026-05-07T00:00:00+00:00",
                "round": 0,
            }],
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _route(text: str, operator: dict | None = None) -> dict:
    """Helper: feed `text` through the message router."""
    return lark_callback.handle_event(
        event_kind="message",
        article_id=None,
        action=None,
        payload={"text": text},
        operator=operator or _OPERATOR,
    )


class _ChromeTestCase(unittest.TestCase):
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
        # Seed an authorized operator with full action grant.
        _write_lark_operator(self.home, _OPERATOR["open_id"], ["*"])

    def tearDown(self) -> None:
        self.stack.close()
        self._tmpdir.cleanup()
        super().tearDown()


# ---------------------------------------------------------------------------
# 12 chrome intents — happy path + false-positive guard
# ---------------------------------------------------------------------------


class ChromeStatusTests(_ChromeTestCase):
    def test_status_keyword_routes_to_chrome_status(self) -> None:
        with patch.object(
            review_triggers, "_emit_lark_status_card"
        ) as mock_emit:
            res = _route("状态")
        mock_emit.assert_called_once()
        self.assertIn("chrome_status_emitted", res["side_effects"])

    def test_status_partial_phrase_does_not_match(self) -> None:
        # "推进到状态 X" must not trigger chrome_status — it's not a whole-text
        # equality match, and existing _classify_intent has "推进" → _advance.
        with patch.object(
            review_triggers, "_emit_lark_status_card"
        ) as mock_emit:
            _route("推进到状态 X")
        mock_emit.assert_not_called()


class ChromeListTests(_ChromeTestCase):
    def test_list_keyword_emits_article_list_card(self) -> None:
        _seed_article(
            self.home, "art1",
            state=review_state.STATE_DRAFT_PENDING_REVIEW,
            title="hello",
        )
        with patch.object(
            review_triggers, "_emit_lark_article_list_card"
        ) as mock_emit:
            res = _route("列表")
        mock_emit.assert_called_once()
        kwargs = mock_emit.call_args.kwargs
        self.assertEqual(kwargs["articles"][0]["article_id"], "art1")
        self.assertIn("chrome_list_emitted", res["side_effects"])

    def test_list_inside_word_does_not_match(self) -> None:
        # "列表项" should not whole-match "列表".
        with patch.object(
            review_triggers, "_emit_lark_article_list_card"
        ) as mock_emit:
            _route("列表项很多")
        mock_emit.assert_not_called()


class ChromePublishedTests(_ChromeTestCase):
    def test_published_keyword_emits_published_list(self) -> None:
        _seed_article(
            self.home, "art2",
            state=review_state.STATE_PUBLISHED,
            title="done",
        )
        # Patch metadata write to add published_at.
        meta_path = self.home / "drafts" / "art2" / "metadata.json"
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        data["published_at"] = "2026-05-01T00:00:00Z"
        meta_path.write_text(json.dumps(data), encoding="utf-8")
        with patch.object(
            review_triggers, "_emit_lark_published_list_card"
        ) as mock_emit:
            res = _route("已发")
        mock_emit.assert_called_once()
        self.assertIn("chrome_published_emitted", res["side_effects"])

    def test_published_in_phrase_does_not_match(self) -> None:
        with patch.object(
            review_triggers, "_emit_lark_published_list_card"
        ) as mock_emit:
            _route("已发现 bug")  # "已发" appears as substring but not whole-text
        mock_emit.assert_not_called()


class ChromeScanTests(_ChromeTestCase):
    def test_scan_keyword_kicks_off_hotspots(self) -> None:
        with patch(
            "agentflow.agent_review.daemon._spawn_hotspots"
        ) as mock_spawn, patch.object(
            review_triggers, "_emit_lark_scan_kicked_card"
        ) as mock_emit:
            res = _route("扫一下")
        mock_spawn.assert_called_once()
        mock_emit.assert_called_once()
        self.assertIn("chrome_scan_spawned", res["side_effects"])

    def test_scan_substring_does_not_match(self) -> None:
        # "扫地了" contains 扫 but not whole-text "扫一下" / "scan".
        with patch(
            "agentflow.agent_review.daemon._spawn_hotspots"
        ) as mock_spawn:
            _route("扫地了")
        mock_spawn.assert_not_called()


class ChromeJobsTests(_ChromeTestCase):
    def test_jobs_keyword_emits_jobs_card(self) -> None:
        with patch.object(
            review_triggers, "_emit_lark_jobs_card"
        ) as mock_emit:
            res = _route("任务")
        mock_emit.assert_called_once()
        self.assertIn("chrome_jobs_emitted", res["side_effects"])

    def test_jobs_inside_phrase_does_not_match(self) -> None:
        with patch.object(
            review_triggers, "_emit_lark_jobs_card"
        ) as mock_emit:
            _route("我有新任务给你")
        mock_emit.assert_not_called()


class ChromeAuditListTests(_ChromeTestCase):
    def test_audit_list_keyword_emits_audit_list_card(self) -> None:
        # Seed a fake audit.jsonl tail.
        audit_path = self.home / "review" / "audit.jsonl"
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text(
            json.dumps({"kind": "callback", "ts": "2026-05-07"}) + "\n",
            encoding="utf-8",
        )
        with patch.object(
            review_triggers, "_emit_lark_audit_list_card"
        ) as mock_emit:
            res = _route("审计列表")
        mock_emit.assert_called_once()
        self.assertIn("chrome_audit_list_emitted", res["side_effects"])

    def test_bare_audit_falls_to_existing_gate_b_diff(self) -> None:
        # Bare "审计" must NOT trigger chrome_audit_list — it's the existing
        # Gate B diff intent. Without an active article it returns
        # "no_active_article" via the existing classifier.
        with patch.object(
            review_triggers, "_emit_lark_audit_list_card"
        ) as mock_emit:
            res = _route("审计")
        mock_emit.assert_not_called()
        # Routes through existing classifier (no_active_article since no
        # pending review article exists).
        self.assertIn("no_active_article", res["side_effects"])


class ChromeAuthDebugTests(_ChromeTestCase):
    def test_auth_debug_keyword_emits_auth_debug_card(self) -> None:
        with patch.object(
            review_triggers, "_emit_lark_auth_debug_card"
        ) as mock_emit:
            res = _route("鉴权")
        mock_emit.assert_called_once()
        self.assertIn("chrome_auth_debug_emitted", res["side_effects"])

    def test_auth_debug_substring_does_not_match(self) -> None:
        # "鉴权策略" contains 鉴权 but is not whole-text equal.
        with patch.object(
            review_triggers, "_emit_lark_auth_debug_card"
        ) as mock_emit:
            _route("鉴权策略 review")
        mock_emit.assert_not_called()


class ChromeSuggestionsTests(_ChromeTestCase):
    def test_suggestions_keyword_emits_suggestion_list(self) -> None:
        with patch(
            "agentflow.shared.topic_profile_lifecycle.list_suggestions",
            return_value=[],
        ), patch.object(
            review_triggers, "_emit_lark_suggestion_list_card"
        ) as mock_emit:
            res = _route("建议")
        mock_emit.assert_called_once()
        self.assertIn("chrome_suggestions_emitted", res["side_effects"])

    def test_suggestions_substring_does_not_match(self) -> None:
        with patch(
            "agentflow.shared.topic_profile_lifecycle.list_suggestions",
            return_value=[],
        ), patch.object(
            review_triggers, "_emit_lark_suggestion_list_card"
        ) as mock_emit:
            _route("建议你试试这个")
        mock_emit.assert_not_called()


class ChromeSkipTests(_ChromeTestCase):
    def test_skip_with_article_id_transitions_to_image_skipped(self) -> None:
        _seed_article(
            self.home, "art3",
            state=review_state.STATE_IMAGE_PENDING_REVIEW,
        )
        res = _route("跳过 art3")
        self.assertIn("chrome_skip_applied", res["side_effects"])
        self.assertEqual(
            review_state.current_state("art3"),
            review_state.STATE_IMAGE_SKIPPED,
        )

    def test_skip_without_arg_does_not_match(self) -> None:
        # "跳过" alone (no id) does not match the verb regex; falls to
        # existing classifier (which has no "跳过" keyword) → unknown_intent.
        res = _route("跳过")
        self.assertNotIn("chrome_skip_applied", res["side_effects"])


class ChromeDeferTests(_ChromeTestCase):
    def test_defer_with_article_id_and_hours(self) -> None:
        _seed_article(
            self.home, "art4",
            state=review_state.STATE_DRAFT_PENDING_REVIEW,
        )
        res = _route("推迟 art4 4h")
        self.assertIn("chrome_defer_applied", res["side_effects"])

    def test_defer_without_hours_does_not_match(self) -> None:
        res = _route("推迟 art4")
        self.assertNotIn("chrome_defer_applied", res["side_effects"])


class ChromePublishMarkTests(_ChromeTestCase):
    def test_publish_mark_transitions_to_published(self) -> None:
        _seed_article(
            self.home, "art5",
            state=review_state.STATE_READY_TO_PUBLISH,
        )
        res = _route("标记已发 art5")
        self.assertIn("chrome_publish_mark_applied", res["side_effects"])
        self.assertEqual(
            review_state.current_state("art5"),
            review_state.STATE_PUBLISHED,
        )

    def test_publish_mark_without_arg_does_not_match(self) -> None:
        res = _route("标记已发")
        self.assertNotIn("chrome_publish_mark_applied", res["side_effects"])


class ChromeCancelTests(_ChromeTestCase):
    def test_cancel_transitions_to_draft_rejected(self) -> None:
        _seed_article(
            self.home, "art6",
            state=review_state.STATE_DRAFT_PENDING_REVIEW,
        )
        res = _route("取消 art6")
        self.assertIn("chrome_cancel_applied", res["side_effects"])
        self.assertEqual(
            review_state.current_state("art6"),
            review_state.STATE_DRAFT_REJECTED,
        )

    def test_cancel_without_arg_does_not_match(self) -> None:
        res = _route("取消")
        self.assertNotIn("chrome_cancel_applied", res["side_effects"])


# ---------------------------------------------------------------------------
# Cross-cutting tests
# ---------------------------------------------------------------------------


class ChromeAuthFailClosedTests(unittest.TestCase):
    """All chrome handlers must respect fail-closed auth (no operator entry → deny)."""

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
        # auth.json exists with a different operator → fail closed for the
        # test operator. (post-L-4: file-absent fail-opens, so we must
        # explicitly seed an auth.json to exercise the fail-closed branch.)
        _write_lark_operator(self.home, "ou_other_admin", ["*"])

    def tearDown(self) -> None:
        self.stack.close()
        self._tmpdir.cleanup()
        super().tearDown()

    def test_unauthorized_open_id_denies_status(self) -> None:
        with patch.object(
            review_triggers, "_emit_lark_status_card"
        ) as mock_emit:
            res = _route("状态")
        mock_emit.assert_not_called()
        self.assertIn("not_authorized", res["side_effects"])

    def test_unauthorized_open_id_denies_skip_mutation(self) -> None:
        # Even with a valid article, an unauthorized operator must be denied
        # before the state.transition call.
        _seed_article(
            self.home, "art7",
            state=review_state.STATE_IMAGE_PENDING_REVIEW,
        )
        res = _route("跳过 art7")
        self.assertIn("not_authorized", res["side_effects"])
        # State must NOT have transitioned.
        self.assertEqual(
            review_state.current_state("art7"),
            review_state.STATE_IMAGE_PENDING_REVIEW,
        )


class ChromeDoesNotShadowAdvanceTests(_ChromeTestCase):
    """The existing ``推进 / advance`` intent must beat any chrome keyword."""

    def test_advance_phrase_routes_to_existing_classifier(self) -> None:
        # "推进到下个 gate" must still go to the _advance branch (which calls
        # _route_advance and emits "no_active_article" since we have none).
        with patch.object(
            review_triggers, "_emit_lark_status_card"
        ) as mock_emit:
            res = _route("推进到下个 gate")
        mock_emit.assert_not_called()
        self.assertIn("no_active_article", res["side_effects"])


class ChromeMutateAuditTests(_ChromeTestCase):
    """Mutating chrome handlers must record an audit entry on the article."""

    def test_skip_records_gate_history(self) -> None:
        _seed_article(
            self.home, "art_audit_skip",
            state=review_state.STATE_IMAGE_PENDING_REVIEW,
        )
        _route("跳过 art_audit_skip")
        history = review_state.gate_history("art_audit_skip")
        self.assertGreaterEqual(len(history), 2)  # seed + chrome transition
        self.assertEqual(history[-1]["decision"], "chrome_skip_via_lark")

    def test_publish_mark_records_gate_history(self) -> None:
        _seed_article(
            self.home, "art_audit_pm",
            state=review_state.STATE_READY_TO_PUBLISH,
        )
        _route("标记已发 art_audit_pm")
        history = review_state.gate_history("art_audit_pm")
        self.assertEqual(history[-1]["decision"], "chrome_publish_mark_via_lark")

    def test_cancel_records_gate_history(self) -> None:
        _seed_article(
            self.home, "art_audit_cancel",
            state=review_state.STATE_DRAFT_PENDING_REVIEW,
        )
        _route("取消 art_audit_cancel")
        history = review_state.gate_history("art_audit_cancel")
        self.assertEqual(history[-1]["decision"], "chrome_cancel_via_lark")


class ChromeWebCommandsTests(unittest.TestCase):
    """All 12 lark_chrome_* commands must be registered in _COMMAND_SPECS."""

    def test_all_chrome_commands_registered(self) -> None:
        commands = review_web._COMMAND_SPECS
        chrome_names = [
            "lark_chrome_status",
            "lark_chrome_list",
            "lark_chrome_published",
            "lark_chrome_scan",
            "lark_chrome_jobs",
            "lark_chrome_audit_list",
            "lark_chrome_auth_debug",
            "lark_chrome_suggestions",
            "lark_chrome_skip",
            "lark_chrome_defer",
            "lark_chrome_publish_mark",
            "lark_chrome_cancel",
        ]
        for name in chrome_names:
            self.assertIn(name, commands, f"missing command: {name}")
            spec = commands[name]
            self.assertTrue(spec["in_process"])
        # Mutating ones are dangerous=True.
        for name in (
            "lark_chrome_skip",
            "lark_chrome_defer",
            "lark_chrome_publish_mark",
            "lark_chrome_cancel",
        ):
            self.assertTrue(
                commands[name]["dangerous"],
                f"{name} should be dangerous=True",
            )
        # Read-only ones are dangerous=False.
        for name in (
            "lark_chrome_status",
            "lark_chrome_list",
            "lark_chrome_published",
            "lark_chrome_scan",
            "lark_chrome_jobs",
            "lark_chrome_audit_list",
            "lark_chrome_auth_debug",
            "lark_chrome_suggestions",
        ):
            self.assertFalse(
                commands[name]["dangerous"],
                f"{name} should be dangerous=False",
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
