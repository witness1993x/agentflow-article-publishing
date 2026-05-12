"""L-4 closure: legacy ``_authorize_or_deny`` → ``_authorize_or_deny_v2``.

Phase 2 hardening tests proving:
1. In-module handlers (here: ``approve_b`` / Gate B) flow through the
   fail-closed v2 path. An unauthorized operator with a configured
   ``auth.json`` is denied.
2. The same handler proceeds normally for an authorized operator.
3. ``auth.json`` ABSENT → fail-OPEN (dev/test friendly default). This is
   the migration-friendliness invariant: existing tests that don't seed
   an auth.json must keep working unchanged.
4. ``auth.json`` PRESENT but ``lark_operators`` empty / missing → fail-CLOSED
   (the IND-3 invariant — explicit configuration intent).
5. The legacy ``_authorize_or_deny`` symbol is preserved as a
   backwards-compat surface; Phase 3 will remove it.

Plan ref: docs/BLOGFLOW_TG_TO_LARK_PARITY.md §11.5 L-4.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from agentflow.agent_review import auth as review_auth
from agentflow.agent_review import lark_callback
from agentflow.shared import bootstrap, memory


_OPERATOR = {"open_id": "ou_l4_operator", "name": "L4Operator"}


def _seed_auth_json(home: Path, payload: dict) -> Path:
    """Write ``review/auth.json`` directly so tests exercise the loader path."""
    review_dir = home / "review"
    review_dir.mkdir(parents=True, exist_ok=True)
    path = review_dir / "auth.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


class _L4TestCase(unittest.TestCase):
    """Tmp AGENTFLOW_HOME isolation mirroring the rest of the lark suite."""

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


class LegacyHandlerUsesV2PathTests(_L4TestCase):
    """Migration smoke test: a Gate-B handler is denied via the v2 gate."""

    def test_legacy_handler_uses_v2_path(self) -> None:
        # Configured auth.json with a different open_id → fail-closed for
        # _OPERATOR. If the legacy ``is_lark_authorized`` path were still
        # in use, the empty ``lark_auth.json`` + no env would let it through.
        _seed_auth_json(self.home, {
            "lark_operators": [
                {"open_id": "ou_other", "name": "Other", "actions": ["*"]},
            ],
        })
        with patch.object(lark_callback.review_state, "transition") as mock_trans:
            res = lark_callback.handle_event(
                event_kind="card_action",
                article_id="art_l4",
                action="approve_b",
                payload={},
                operator=_OPERATOR,
            )
        self.assertIn("not_authorized", res["side_effects"])
        self.assertIsNotNone(res["reply_card"])
        self.assertEqual(res["reply_card"]["header"]["template"], "red")
        # State must NOT have transitioned — the gate fired before the body.
        mock_trans.assert_not_called()

    def test_legacy_handler_works_with_authorized_operator(self) -> None:
        # Same handler, but the operator IS in lark_operators with the
        # required ``review`` action (approve_b → review).
        _seed_auth_json(self.home, {
            "lark_operators": [
                {
                    "open_id": _OPERATOR["open_id"],
                    "name": _OPERATOR["name"],
                    "actions": ["review", "edit", "publish"],
                },
            ],
        })
        with patch.object(lark_callback.review_state, "transition") as mock_trans:
            res = lark_callback.handle_event(
                event_kind="card_action",
                article_id="art_l4",
                action="approve_b",
                payload={},
                operator=_OPERATOR,
            )
        self.assertNotIn("not_authorized", res["side_effects"])
        mock_trans.assert_called_once()


class AuthJsonFileStateTests(_L4TestCase):
    """The 3-state auth.json behavior contract."""

    def test_auth_json_missing_falls_back_to_allow(self) -> None:
        # No auth.json on disk — file-absent fail-OPEN keeps existing test
        # fixtures (and fresh dev checkouts) working.
        auth_path = self.home / "review" / "auth.json"
        self.assertFalse(auth_path.exists())
        self.assertTrue(
            review_auth.is_authorized_open_id("ou_anything", "publish")
        )
        self.assertTrue(
            review_auth.is_authorized_open_id(_OPERATOR["open_id"], "review")
        )
        # Still rejects None / empty open_id even when fail-open.
        self.assertFalse(review_auth.is_authorized_open_id(None, "review"))
        self.assertFalse(review_auth.is_authorized_open_id("", "review"))

    def test_auth_json_with_empty_lark_operators_section_denies(self) -> None:
        # File exists, lark_operators empty → fail-CLOSED. Operators must
        # explicitly opt in by adding entries.
        _seed_auth_json(self.home, {"lark_operators": []})
        self.assertFalse(
            review_auth.is_authorized_open_id("ou_anything", "review")
        )
        self.assertFalse(
            review_auth.is_authorized_open_id(_OPERATOR["open_id"], "publish")
        )

        # File with the section absent (TG-only config) must also fail-closed.
        _seed_auth_json(self.home, {
            "authorized_uids": [{"uid": 1, "allowed_actions": ["*"]}],
        })
        self.assertFalse(
            review_auth.is_authorized_open_id("ou_anything", "review")
        )


class LegacyCompatSurfaceTests(_L4TestCase):
    """``_authorize_or_deny`` must remain importable through Phase 2."""

    def test_legacy_authorize_or_deny_function_still_exists(self) -> None:
        # Phase 3 will remove this — until then the symbol is a frozen
        # backwards-compat surface for adapters/tests outside the module.
        self.assertTrue(hasattr(lark_callback, "_authorize_or_deny"))
        self.assertTrue(callable(lark_callback._authorize_or_deny))
        # Distinct from v2 (we did not collapse them).
        self.assertIsNot(
            lark_callback._authorize_or_deny,
            lark_callback._authorize_or_deny_v2,
        )
        # Docstring flags it as legacy so future readers don't add new callers.
        doc = (lark_callback._authorize_or_deny.__doc__ or "").lower()
        self.assertIn("legacy", doc)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
