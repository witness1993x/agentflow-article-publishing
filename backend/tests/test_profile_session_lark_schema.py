"""Tests for Lark-side profile-session schema additions.

Covers GAP-P2 / IND-6: ``active_open_id`` + ``active_lark_chat_id``
fields, the parallel claim/release/find helpers, and the v2 migration
helper. Mirrors the TG-side invariant that a session is only "active"
when ``status == "collecting"`` AND the surface-specific operator id
matches.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from agentflow.shared import bootstrap
from agentflow.shared.topic_profile_lifecycle import (
    claim_session_lark,
    constraint_sessions_dir,
    find_active_session_for_uid,
    find_active_session_lark,
    load_session,
    migrate_session_schema_v2,
    release_session_lark,
    save_session,
    session_path,
)


class _ProfileSessionLarkBase(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.home = Path(self._tmpdir.name)
        self.stack = ExitStack()
        self.stack.enter_context(
            patch.object(bootstrap, "AGENTFLOW_HOME", self.home)
        )
        bootstrap.ensure_user_dirs()

    def tearDown(self) -> None:
        self.stack.close()
        self._tmpdir.cleanup()
        super().tearDown()

    def _seed(self, session_id: str, **extra) -> dict:
        payload = {
            "id": session_id,
            "profile_id": "default",
            "mode": "init",
            "status": "pending",
            "step_index": 0,
            "answers": {},
        }
        payload.update(extra)
        save_session(payload)
        return load_session(session_id)


class ClaimSessionLarkTests(_ProfileSessionLarkBase):
    def test_claim_session_lark_sets_open_id_and_chat(self) -> None:
        self._seed("session_lark_claim_a")

        result = claim_session_lark(
            "session_lark_claim_a",
            open_id="ou_alice",
            lark_chat_id="oc_alice_chat",
        )

        self.assertEqual(result["status"], "collecting")
        self.assertEqual(result["active_open_id"], "ou_alice")
        self.assertEqual(result["active_lark_chat_id"], "oc_alice_chat")

        # Round-trip from disk to confirm persistence.
        on_disk = load_session("session_lark_claim_a")
        self.assertEqual(on_disk["status"], "collecting")
        self.assertEqual(on_disk["active_open_id"], "ou_alice")
        self.assertEqual(on_disk["active_lark_chat_id"], "oc_alice_chat")


class FindActiveSessionLarkTests(_ProfileSessionLarkBase):
    def test_find_active_session_by_open_id(self) -> None:
        self._seed("session_lark_find_a")
        claim_session_lark(
            "session_lark_find_a",
            open_id="ou_alice",
            lark_chat_id="oc_alice_chat",
        )

        hit = find_active_session_lark("ou_alice")
        self.assertIsNotNone(hit)
        assert hit is not None  # for type-checkers
        self.assertEqual(hit["id"], "session_lark_find_a")
        self.assertEqual(hit["active_open_id"], "ou_alice")

        miss = find_active_session_lark("ou_bob")
        self.assertIsNone(miss)

    def test_active_uid_and_open_id_dont_collide(self) -> None:
        # Session claimed via the TG path: active_uid set, active_open_id
        # remains None. A Lark lookup must NOT match it even if some
        # numeric coincidence existed.
        self._seed(
            "session_tg_only",
            status="collecting",
            active_uid=12345,
            active_chat_id=67890,
            active_open_id=None,
            active_lark_chat_id=None,
        )

        # TG lookup hits.
        tg_hit = find_active_session_for_uid(12345)
        self.assertIsNotNone(tg_hit)
        assert tg_hit is not None
        self.assertEqual(tg_hit["id"], "session_tg_only")

        # Lark lookups must NOT confuse the numeric uid with an open_id.
        self.assertIsNone(find_active_session_lark("12345"))
        self.assertIsNone(find_active_session_lark("ou_anyone"))
        # An empty open_id never matches anything.
        self.assertIsNone(find_active_session_lark(""))


class ReleaseSessionLarkTests(_ProfileSessionLarkBase):
    def test_release_session_lark_clears_open_id(self) -> None:
        self._seed("session_lark_release_a")
        claim_session_lark(
            "session_lark_release_a",
            open_id="ou_carol",
            lark_chat_id="oc_carol_chat",
        )

        # Sanity: it's findable while collecting.
        self.assertIsNotNone(find_active_session_lark("ou_carol"))

        released = release_session_lark("session_lark_release_a")

        self.assertIsNone(released["active_open_id"])
        self.assertIsNone(released["active_lark_chat_id"])
        self.assertNotEqual(released["status"], "collecting")
        # Default release status is "released".
        self.assertEqual(released["status"], "released")

        # No longer findable.
        self.assertIsNone(find_active_session_lark("ou_carol"))


class MigrateSessionSchemaV2Tests(_ProfileSessionLarkBase):
    def test_migrate_v2_idempotent(self) -> None:
        # Hand-craft a session file that only has the *old* fields,
        # bypassing claim helpers so the new keys are genuinely absent.
        sid = "session_legacy"
        payload = {
            "id": sid,
            "profile_id": "default",
            "mode": "init",
            "status": "collecting",
            "active_uid": 999,
            "active_chat_id": 111,
            "step_index": 0,
            "answers": {},
        }
        path = session_path(sid)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        # Pre-condition: new keys really are absent.
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotIn("active_open_id", on_disk)
        self.assertNotIn("active_lark_chat_id", on_disk)

        first = migrate_session_schema_v2(path)
        self.assertIn("active_open_id", first)
        self.assertIn("active_lark_chat_id", first)
        self.assertIsNone(first["active_open_id"])
        self.assertIsNone(first["active_lark_chat_id"])
        # Existing fields preserved.
        self.assertEqual(first["active_uid"], 999)
        self.assertEqual(first["status"], "collecting")

        # Second run: must not crash and must leave shape intact.
        second = migrate_session_schema_v2(path)
        self.assertIn("active_open_id", second)
        self.assertIn("active_lark_chat_id", second)
        self.assertIsNone(second["active_open_id"])
        self.assertIsNone(second["active_lark_chat_id"])
        self.assertEqual(second["active_uid"], 999)


if __name__ == "__main__":  # pragma: no cover - convenience runner
    unittest.main()
