"""Tests for GAP-AUDIT-LIST (`lark_view_audit_recent`).

Covers:
* `_handle_view_audit_recent` happy paths (default n, n param, kind filter,
  n-clamp at MAX=100).
* Fail-closed auth (unauthorized operator gets a deny card).
* DRY check: chrome free-text path delegates to `_handle_view_audit_recent`.
* `lark_view_audit_recent` is registered in `_LARK_COMMANDS`.

Plan ref: docs/BLOGFLOW_TG_TO_LARK_PARITY.md §3.5 GAP-AUDIT-LIST.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from agentflow.agent_review import lark_callback
from agentflow.agent_review import triggers as review_triggers
from agentflow.agent_review import web as review_web
from agentflow.shared import bootstrap, memory


_OPERATOR = {"open_id": "ou_audit_op", "name": "AuditTester"}


def _write_lark_operator(home: Path, open_id: str, actions: list[str]) -> None:
    review_dir = home / "review"
    review_dir.mkdir(parents=True, exist_ok=True)
    auth_path = review_dir / "auth.json"
    auth_path.write_text(
        json.dumps({
            "lark_operators": [
                {"open_id": open_id, "name": "AuditTester", "actions": actions},
            ],
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _seed_audit_jsonl(home: Path, entries: list[dict]) -> None:
    p = home / "review" / "audit.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as fh:
        for ev in entries:
            fh.write(json.dumps(ev, ensure_ascii=False) + "\n")


class _AuditListTestCase(unittest.TestCase):
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
        _write_lark_operator(self.home, _OPERATOR["open_id"], ["*"])

    def tearDown(self) -> None:
        self.stack.close()
        self._tmpdir.cleanup()
        super().tearDown()


class HandleViewAuditRecentTests(_AuditListTestCase):
    def test_handle_view_audit_recent_emits_card_with_default_n(self) -> None:
        # Seed 30 entries; default n=20 should keep newest 20.
        _seed_audit_jsonl(
            self.home,
            [{"kind": "callback", "audit_ts": f"2026-05-07T00:00:{i:02d}Z"} for i in range(30)],
        )
        with patch.object(review_triggers, "_emit_lark_audit_list_card") as mock_emit:
            res = lark_callback._handle_view_audit_recent(
                operator=_OPERATOR, payload={}, article_id=None,
            )
        mock_emit.assert_called_once()
        call_kwargs = mock_emit.call_args.kwargs
        self.assertEqual(len(call_kwargs["entries"]), 20)
        self.assertEqual(call_kwargs["n"], 20)
        self.assertIsNone(call_kwargs.get("filter_kind"))
        self.assertIn("audit_list_emitted", res["side_effects"])

    def test_handle_view_audit_recent_respects_n_param(self) -> None:
        _seed_audit_jsonl(
            self.home,
            [{"kind": "callback", "audit_ts": f"2026-05-07T00:00:{i:02d}Z"} for i in range(20)],
        )
        with patch.object(review_triggers, "_emit_lark_audit_list_card") as mock_emit:
            lark_callback._handle_view_audit_recent(
                operator=_OPERATOR, payload={"n": 5}, article_id=None,
            )
        self.assertEqual(len(mock_emit.call_args.kwargs["entries"]), 5)
        self.assertEqual(mock_emit.call_args.kwargs["n"], 5)

    def test_handle_view_audit_recent_filters_by_kind(self) -> None:
        # Mix of kinds; filter should keep only spawn_failure.
        entries = []
        for i in range(10):
            entries.append({"kind": "callback", "audit_ts": f"2026-05-07T00:00:{i:02d}Z"})
            entries.append({"kind": "spawn_failure", "audit_ts": f"2026-05-07T00:00:{i:02d}Z"})
        _seed_audit_jsonl(self.home, entries)
        with patch.object(review_triggers, "_emit_lark_audit_list_card") as mock_emit:
            lark_callback._handle_view_audit_recent(
                operator=_OPERATOR,
                payload={"kind": "spawn_failure", "n": 20},
                article_id=None,
            )
        emitted = mock_emit.call_args.kwargs["entries"]
        self.assertTrue(emitted)
        for ev in emitted:
            self.assertEqual(ev["kind"], "spawn_failure")
        self.assertEqual(mock_emit.call_args.kwargs["filter_kind"], "spawn_failure")

    def test_handle_view_audit_recent_unauthorized_returns_deny(self) -> None:
        # Operator that's NOT in the whitelist.
        bad_op = {"open_id": "ou_outsider", "name": "NoAuth"}
        _seed_audit_jsonl(
            self.home,
            [{"kind": "callback", "audit_ts": "2026-05-07T00:00:00Z"}],
        )
        with patch.object(review_triggers, "_emit_lark_audit_list_card") as mock_emit:
            res = lark_callback._handle_view_audit_recent(
                operator=bad_op, payload={}, article_id=None,
            )
        mock_emit.assert_not_called()
        self.assertIn("not_authorized", res["side_effects"])
        self.assertIsNotNone(res["reply_card"])

    def test_handle_view_audit_recent_clamps_n_to_max_100(self) -> None:
        _seed_audit_jsonl(
            self.home,
            [{"kind": "callback", "audit_ts": f"2026-05-07T00:00:{i:02d}Z"} for i in range(5)],
        )
        with patch.object(review_triggers, "_emit_lark_audit_list_card") as mock_emit:
            lark_callback._handle_view_audit_recent(
                operator=_OPERATOR, payload={"n": 9999}, article_id=None,
            )
        # Capped at 100 even though input was 9999.
        self.assertEqual(mock_emit.call_args.kwargs["n"], 100)


class ChromePathDelegatesTests(_AuditListTestCase):
    def test_chrome_audit_list_calls_view_audit_recent(self) -> None:
        _seed_audit_jsonl(
            self.home,
            [{"kind": "callback", "audit_ts": "2026-05-07T00:00:00Z"}],
        )
        # Spy on the unified handler to confirm the chrome path delegates.
        with patch.object(
            lark_callback, "_handle_view_audit_recent",
            wraps=lark_callback._handle_view_audit_recent,
        ) as spy, patch.object(
            review_triggers, "_emit_lark_audit_list_card"
        ):
            res = lark_callback._handle_chrome_audit_list(_OPERATOR, {})
        spy.assert_called_once()
        # Legacy chrome side-effect token preserved (chrome happy-path test).
        self.assertIn("chrome_audit_list_emitted", res["side_effects"])
        # New unified token also present.
        self.assertIn("audit_list_emitted", res["side_effects"])


class WebCommandRegistrationTests(unittest.TestCase):
    def test_lark_view_audit_recent_registered_in_web_commands(self) -> None:
        commands = review_web._COMMAND_SPECS
        self.assertIn("lark_view_audit_recent", commands)
        spec = commands["lark_view_audit_recent"]
        self.assertEqual(spec["scope"], "read")
        self.assertTrue(spec["in_process"])
        self.assertFalse(spec["dangerous"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
