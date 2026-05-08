"""L-3 (Wave 2 closure): chrome_defer must really schedule the deferred repost.

Pre-fix, ``_handle_chrome_defer`` only emitted a misleading ack card and
appended a memory event — the gate card never reposted. This module locks
in that the chrome free-text defer path now writes a real entry into the
deferred-repost store, the same one consumed by daemon's
``_drain_deferred_reposts``. Surface routing is design (b): the daemon
sweeper calls ``triggers.post_gate_b/c`` which already dual-emit on TG +
Lark, so no schema change is required.

Plan ref: docs/BLOGFLOW_TG_TO_LARK_PARITY.md §11.5 L-3.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from agentflow.agent_review import daemon as review_daemon
from agentflow.agent_review import lark_callback
from agentflow.agent_review import state as review_state
from agentflow.shared import bootstrap, memory


_OPERATOR = {"open_id": "ou_op_test", "name": "Tester"}


def _write_lark_operator(home: Path, open_id: str, actions: list[str]) -> None:
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


class _ChromeDeferTestCase(unittest.TestCase):
    """Common AGENTFLOW_HOME tmp + memory + _REVIEW_HOME redirect setup."""

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
        # _REVIEW_HOME is computed at daemon import time → must patch for the
        # deferred-repost store to land under the test tmpdir.
        self.stack.enter_context(
            patch.object(review_daemon, "_REVIEW_HOME", self.home / "review")
        )
        bootstrap.ensure_user_dirs()
        _write_lark_operator(self.home, _OPERATOR["open_id"], ["*"])

    def tearDown(self) -> None:
        self.stack.close()
        self._tmpdir.cleanup()
        super().tearDown()

    def _store_path(self) -> Path:
        return self.home / "review" / "deferred_reposts.json"


class ChromeDeferWritesToStoreTests(_ChromeDeferTestCase):
    def test_chrome_defer_writes_to_deferred_store(self) -> None:
        _seed_article(
            self.home, "art_defer_b",
            state=review_state.STATE_DRAFT_PENDING_REVIEW,
        )
        res = lark_callback._handle_chrome_defer(
            _OPERATOR,
            {"text": "推迟 art_defer_b 4h"},
            article_id="art_defer_b",
            hours=4.0,
        )
        self.assertIn("chrome_defer_applied", res["side_effects"])
        self.assertNotIn("schedule_failed", res["side_effects"])

        # Real store entry must exist.
        self.assertTrue(self._store_path().exists())
        scheduled = json.loads(self._store_path().read_text(encoding="utf-8"))
        self.assertEqual(len(scheduled), 1)
        entry = scheduled[0]
        self.assertEqual(entry["gate"], "B")
        self.assertEqual(entry["article_id"], "art_defer_b")
        self.assertAlmostEqual(entry["hours"], 4.0)
        self.assertIn("due_at", entry)
        self.assertTrue(entry["source_short_id"].startswith("lark_chrome:"))

    def test_chrome_defer_gate_c_for_image_pending_review(self) -> None:
        _seed_article(
            self.home, "art_defer_c",
            state=review_state.STATE_IMAGE_PENDING_REVIEW,
        )
        lark_callback._handle_chrome_defer(
            _OPERATOR,
            {"text": "推迟 art_defer_c 1h"},
            article_id="art_defer_c",
            hours=1.0,
        )
        scheduled = json.loads(self._store_path().read_text(encoding="utf-8"))
        self.assertEqual(scheduled[0]["gate"], "C")


class ChromeDeferAckTests(_ChromeDeferTestCase):
    def test_chrome_defer_acks_with_scheduled_time(self) -> None:
        _seed_article(
            self.home, "art_ack",
            state=review_state.STATE_DRAFT_PENDING_REVIEW,
        )
        res = lark_callback._handle_chrome_defer(
            _OPERATOR,
            {"text": "推迟 art_ack 4h"},
            article_id="art_ack",
            hours=4.0,
        )
        card = res.get("reply_card") or {}
        # Title or body must mention the hours so operator knows it landed.
        rendered = json.dumps(card, ensure_ascii=False)
        self.assertIn("4", rendered)
        self.assertIn("已推迟", rendered)


class ChromeDeferUnauthorizedTests(unittest.TestCase):
    """Fail-closed regression: unauthorized operator must NOT mutate the store."""

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
        self.stack.enter_context(
            patch.object(review_daemon, "_REVIEW_HOME", self.home / "review")
        )
        bootstrap.ensure_user_dirs()
        # Different operator authorized → fail closed for _OPERATOR.
        _write_lark_operator(self.home, "ou_other_admin", ["*"])

    def tearDown(self) -> None:
        self.stack.close()
        self._tmpdir.cleanup()
        super().tearDown()

    def test_chrome_defer_unauthorized_returns_deny(self) -> None:
        _seed_article(
            self.home, "art_unauth",
            state=review_state.STATE_DRAFT_PENDING_REVIEW,
        )
        res = lark_callback._handle_chrome_defer(
            _OPERATOR,
            {"text": "推迟 art_unauth 4h"},
            article_id="art_unauth",
            hours=4.0,
        )
        self.assertIn("not_authorized", res["side_effects"])
        self.assertNotIn("chrome_defer_applied", res["side_effects"])
        # Store file must NOT have been written.
        store = self.home / "review" / "deferred_reposts.json"
        self.assertFalse(store.exists())


class ChromeDeferInvalidArticleTests(_ChromeDeferTestCase):
    def test_chrome_defer_invalid_article_id_returns_deny(self) -> None:
        # Note: _resolve_article_for_chrome falls back to the active article
        # if `raw_id` is empty, but a non-empty unknown id must not resolve.
        res = lark_callback._handle_chrome_defer(
            _OPERATOR,
            {"text": "推迟 art_does_not_exist 4h"},
            article_id="art_does_not_exist",
            hours=4.0,
        )
        # Either "missing_article_id" (resolver returned None) or
        # "wrong_state" (resolver returned id but no state) must surface,
        # AND no store mutation.
        self.assertNotIn("chrome_defer_applied", res["side_effects"])
        store = self.home / "review" / "deferred_reposts.json"
        # Empty list or missing file are both acceptable — what matters is
        # no real entry was queued.
        if store.exists():
            scheduled = json.loads(store.read_text(encoding="utf-8"))
            self.assertEqual(scheduled, [])

    def test_chrome_defer_wrong_state_does_not_schedule(self) -> None:
        # Article exists, but state is not *_pending_review → defer should
        # surface "wrong_state" and not write to the store.
        _seed_article(
            self.home, "art_wrong",
            state=review_state.STATE_DRAFTING,
        )
        res = lark_callback._handle_chrome_defer(
            _OPERATOR,
            {"text": "推迟 art_wrong 4h"},
            article_id="art_wrong",
            hours=4.0,
        )
        self.assertIn("wrong_state", res["side_effects"])
        self.assertNotIn("chrome_defer_applied", res["side_effects"])
        store = self.home / "review" / "deferred_reposts.json"
        self.assertFalse(store.exists())


class ChromeDeferDelegationTests(_ChromeDeferTestCase):
    """Confirm the wiring goes through the daemon module's ``_schedule_deferred_repost``
    (the single source of truth — same path as TG /defer + lark_defer button).
    """

    def test_chrome_defer_calls_schedule_deferred_repost(self) -> None:
        _seed_article(
            self.home, "art_delegate",
            state=review_state.STATE_CHANNEL_PENDING_REVIEW,
        )
        with patch.object(
            review_daemon, "_schedule_deferred_repost"
        ) as mock_sched:
            res = lark_callback._handle_chrome_defer(
                _OPERATOR,
                {"text": "推迟 art_delegate 6h"},
                article_id="art_delegate",
                hours=6.0,
            )
        self.assertIn("chrome_defer_applied", res["side_effects"])
        mock_sched.assert_called_once()
        kwargs = mock_sched.call_args.kwargs
        self.assertEqual(kwargs["gate"], "D")
        self.assertEqual(kwargs["article_id"], "art_delegate")
        self.assertIsNone(kwargs["batch_path"])
        self.assertAlmostEqual(kwargs["hours"], 6.0)
        self.assertTrue(str(kwargs["source_sid"]).startswith("lark_chrome:"))


class LarkDeferButtonWritesToStoreTests(_ChromeDeferTestCase):
    """Coverage for the parallel `lark_defer` button path — the per-card 推迟
    button on Gate A/B/C/D. L-3 follow-up: this used to be ack-only too.
    """

    def test_lark_defer_button_writes_to_deferred_store(self) -> None:
        _seed_article(
            self.home, "art_btn_b",
            state=review_state.STATE_DRAFT_PENDING_REVIEW,
        )
        res = lark_callback._handle_defer(
            article_id="art_btn_b",
            operator=_OPERATOR,
            payload={"gate": "B", "hours": 2},
        )
        self.assertIn("deferred", res["side_effects"])
        self.assertNotIn("schedule_failed", res["side_effects"])
        self.assertTrue(self._store_path().exists())
        scheduled = json.loads(self._store_path().read_text(encoding="utf-8"))
        self.assertEqual(len(scheduled), 1)
        entry = scheduled[0]
        self.assertEqual(entry["gate"], "B")
        self.assertEqual(entry["article_id"], "art_btn_b")
        self.assertAlmostEqual(entry["hours"], 2.0)
        self.assertTrue(entry["source_short_id"].startswith("lark_button:"))

    def test_lark_defer_button_rejects_invalid_gate(self) -> None:
        res = lark_callback._handle_defer(
            article_id="art_x",
            operator=_OPERATOR,
            payload={"gate": "Z", "hours": 4},
        )
        self.assertIn("bad_gate", res["side_effects"])
        self.assertFalse(self._store_path().exists())

    def test_lark_defer_button_default_hours_is_4(self) -> None:
        _seed_article(
            self.home, "art_btn_default",
            state=review_state.STATE_IMAGE_PENDING_REVIEW,
        )
        lark_callback._handle_defer(
            article_id="art_btn_default",
            operator=_OPERATOR,
            payload={"gate": "C"},  # no hours
        )
        scheduled = json.loads(self._store_path().read_text(encoding="utf-8"))
        self.assertAlmostEqual(scheduled[0]["hours"], 4.0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
