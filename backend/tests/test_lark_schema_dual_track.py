"""Tests for IND-1 / IND-2 — Lark surface fields on gate_history and short_id.

Covers the dual-track schema additions made for the TG -> Lark parity
migration: ``state.transition()`` accepts optional ``lark_chat_id`` /
``lark_card_id``, and ``short_id.attach_lark_card()`` mirrors
``attach_message_id()`` for the Lark surface. Both surface families must be
independently optional and coexistable on a single entry (dual-emission).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from agentflow.agent_review import short_id as review_short_id
from agentflow.agent_review import state as review_state
from agentflow.shared import bootstrap


class _AgentflowHomeTestCase(unittest.TestCase):
    """Minimal isolation harness — patches ``AGENTFLOW_HOME`` at a tmp dir.

    Mirrors the pattern used in ``tests/test_v02_workflows.py`` but trimmed
    to what these schema-only tests need.
    """

    def setUp(self) -> None:
        super().setUp()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.home = Path(self._tmpdir.name)
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(bootstrap, "AGENTFLOW_HOME", self.home))
        bootstrap.ensure_user_dirs()

    def tearDown(self) -> None:
        self.stack.close()
        self._tmpdir.cleanup()
        super().tearDown()

    def _seed_article(self, article_id: str = "art_001") -> str:
        """Create a minimal metadata.json so ``state`` helpers can read it."""
        draft_dir = self.home / "drafts" / article_id
        draft_dir.mkdir(parents=True, exist_ok=True)
        (draft_dir / "metadata.json").write_text(
            json.dumps({"gate_history": []}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return article_id


class GateHistoryLarkFieldsTests(_AgentflowHomeTestCase):
    def test_gate_history_accepts_lark_fields(self) -> None:
        """A Lark-surface transition write preserves both lark_* fields."""
        article_id = self._seed_article()
        review_state.transition(
            article_id,
            gate="A",
            to_state=review_state.STATE_TOPIC_APPROVED,
            actor="human",
            decision="approve",
            lark_chat_id="oc_lark_chat_42",
            lark_card_id="om_xxx",
        )
        history = review_state.gate_history(article_id)
        self.assertEqual(len(history), 1)
        entry = history[0]
        self.assertEqual(entry["lark_chat_id"], "oc_lark_chat_42")
        self.assertEqual(entry["lark_card_id"], "om_xxx")
        # No tg_* keys should be set when only Lark fields were passed.
        self.assertNotIn("tg_chat_id", entry)
        self.assertNotIn("tg_message_id", entry)

    def test_gate_history_backward_compat(self) -> None:
        """Reading a TG-only entry (no lark_* keys) must not raise."""
        article_id = self._seed_article("art_002")
        # Hand-craft a minimal legacy entry with only tg_* surface fields.
        meta_path = self.home / "drafts" / article_id / "metadata.json"
        legacy_entry = {
            "gate": "B",
            "from_state": review_state.STATE_TOPIC_POOL,
            "to_state": review_state.STATE_TOPIC_APPROVED,
            "actor": "human",
            "decision": "approve",
            "timestamp": "2026-04-25T08:00:00+00:00",
            "round": 0,
            "tg_chat_id": 123456,
            "tg_message_id": 789,
        }
        meta_path.write_text(
            json.dumps({"gate_history": [legacy_entry]}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        # Both read paths should tolerate the missing lark_* keys.
        self.assertEqual(
            review_state.current_state(article_id),
            review_state.STATE_TOPIC_APPROVED,
        )
        history = review_state.gate_history(article_id)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["tg_chat_id"], 123456)
        self.assertEqual(history[0]["tg_message_id"], 789)
        self.assertNotIn("lark_chat_id", history[0])
        self.assertNotIn("lark_card_id", history[0])

        # And appending a fresh Lark-only transition on top of a legacy
        # history does not break the existing entry or the new one.
        review_state.transition(
            article_id,
            gate="B",
            to_state=review_state.STATE_DRAFTING,
            actor="daemon",
            decision="approve",
            lark_chat_id="oc_legacy_mix",
            lark_card_id="om_legacy_mix",
        )
        history = review_state.gate_history(article_id)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["tg_message_id"], 789)
        self.assertNotIn("lark_card_id", history[0])
        self.assertEqual(history[1]["lark_card_id"], "om_legacy_mix")


class ShortIdAttachLarkCardTests(_AgentflowHomeTestCase):
    def test_short_id_attach_lark_card(self) -> None:
        """Registering and attaching Lark card identity round-trips."""
        sid = review_short_id.register(gate="B", article_id="art_lark_only")
        ok = review_short_id.attach_lark_card(
            sid,
            lark_card_id="om_card_001",
            lark_chat_id="oc_chat_001",
        )
        self.assertTrue(ok)
        entry = review_short_id.resolve(sid)
        self.assertIsNotNone(entry)
        assert entry is not None  # for type-checkers
        self.assertEqual(entry["lark_card_id"], "om_card_001")
        self.assertEqual(entry["lark_chat_id"], "oc_chat_001")
        # No tg_message_id should appear when only Lark was attached.
        self.assertNotIn("tg_message_id", entry)

    def test_short_id_dual_attach(self) -> None:
        """``attach_message_id`` and ``attach_lark_card`` both stamp the same entry."""
        sid = review_short_id.register(gate="B", article_id="art_dual")
        self.assertTrue(review_short_id.attach_message_id(sid, 4242))
        self.assertTrue(
            review_short_id.attach_lark_card(
                sid,
                lark_card_id="om_dual_xxx",
                lark_chat_id="oc_dual_chat",
            )
        )
        entry = review_short_id.resolve(sid)
        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry["tg_message_id"], 4242)
        self.assertEqual(entry["lark_card_id"], "om_dual_xxx")
        self.assertEqual(entry["lark_chat_id"], "oc_dual_chat")
        # The original article_id pointer must be preserved alongside the
        # surface identifiers.
        self.assertEqual(entry["article_id"], "art_dual")

    def test_attach_lark_card_rejects_empty_identifiers(self) -> None:
        """Defensive: empty card_id or chat_id must not silently mutate the entry."""
        sid = review_short_id.register(gate="B", article_id="art_empty")
        self.assertFalse(
            review_short_id.attach_lark_card(sid, lark_card_id="", lark_chat_id="oc")
        )
        self.assertFalse(
            review_short_id.attach_lark_card(sid, lark_card_id="om", lark_chat_id="")
        )
        entry = review_short_id.resolve(sid)
        assert entry is not None
        self.assertNotIn("lark_card_id", entry)
        self.assertNotIn("lark_chat_id", entry)


if __name__ == "__main__":
    unittest.main()
