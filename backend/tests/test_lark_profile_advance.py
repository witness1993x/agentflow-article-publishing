"""Tests for the Lark Profile multi-turn follow-up flow (GAP-P2).

Covers:
* ``review_triggers._emit_lark_profile_question_card`` event emission.
* ``lark_callback._handle_profile_advance`` happy path: write answer, emit
  next question, and on completion release session + emit
  ``notify.profile_setup_done``.
* Auth fail-closed via ``_authorize_or_deny_v2``.
* Empty-answer deny path.
* Existing-session resume (no re-claim when operator already claimed).
* ``web._LARK_COMMANDS`` registration of ``lark_profile_advance``.

Plan ref: docs/BLOGFLOW_TG_TO_LARK_PARITY.md §3.2 GAP-P2.
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
from agentflow.shared import topic_profile_lifecycle as lifecycle


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


def _seed_session(
    *,
    session_id: str,
    profile_id: str,
    missing_fields: list[str],
    collected: dict[str, str] | None = None,
    active_open_id: str | None = None,
    active_lark_chat_id: str | None = None,
    status: str = "collecting",
) -> Path:
    """Write a profile session JSON with the schema v2 fields populated."""
    payload = {
        "id": session_id,
        "profile_id": profile_id,
        "status": status,
        "missing_fields": list(missing_fields),
        "collected": dict(collected or {}),
        "active_open_id": active_open_id,
        "active_lark_chat_id": active_lark_chat_id,
        "active_uid": None,
        "active_chat_id": None,
        "schema_version": 1,
    }
    return lifecycle.save_session(payload)


class _ProfileAdvanceTestCase(unittest.TestCase):
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
# _emit_lark_profile_question_card
# ---------------------------------------------------------------------------


class EmitProfileQuestionCardTests(_ProfileAdvanceTestCase):
    def test_emit_profile_question_card_skips_when_lark_not_primary(self) -> None:
        with patch.dict(
            os.environ, {"AGENTFLOW_LARK_APP_PRIMARY": "false"}, clear=False
        ), patch("agentflow.shared.agent_bridge.emit_agent_event") as emit_mock:
            review_triggers._emit_lark_profile_question_card(
                session_path="/tmp/session_x.json",
                profile_id="p1",
                question_field="search_queries",
                question_text="请输入 search queries",
                question_index=1,
                total_questions=3,
            )
        emit_mock.assert_not_called()

    def test_emit_profile_question_card_emits_review_event_with_question_fields(self) -> None:
        with patch.dict(
            os.environ, {"AGENTFLOW_LARK_APP_PRIMARY": "true"}, clear=False
        ), patch("agentflow.shared.agent_bridge.emit_agent_event") as emit_mock:
            review_triggers._emit_lark_profile_question_card(
                session_path="/tmp/session_x.json",
                profile_id="p1",
                question_field="search_queries",
                question_text="请输入 search queries",
                question_index=1,
                total_questions=3,
            )
        emit_mock.assert_called_once()
        kwargs = emit_mock.call_args.kwargs
        self.assertEqual(kwargs["event_type"], "review.profile_setup_card")
        self.assertEqual(kwargs["article_id"], "p1")
        payload = kwargs["payload"]
        self.assertEqual(payload["gate"], "P")
        self.assertEqual(payload["profile_id"], "p1")
        self.assertEqual(payload["session_path"], "/tmp/session_x.json")
        self.assertEqual(payload["current_question"], "请输入 search queries")
        self.assertEqual(payload["question_field"], "search_queries")
        self.assertEqual(payload["question_index"], 1)
        self.assertEqual(payload["total_questions"], 3)
        commands = [a["command"] for a in payload["actions"]]
        self.assertIn("lark_profile_advance", commands)
        self.assertIn("lark_defer", commands)
        # The advance button must carry the question_field so the daemon
        # knows where to write the answer.
        advance = next(
            a for a in payload["actions"] if a["command"] == "lark_profile_advance"
        )
        self.assertEqual(advance["payload"]["question_field"], "search_queries")
        self.assertEqual(advance["payload"]["session_path"], "/tmp/session_x.json")


# ---------------------------------------------------------------------------
# _handle_profile_advance happy paths
# ---------------------------------------------------------------------------


class HandleProfileAdvanceWriteTests(_ProfileAdvanceTestCase):
    def test_handle_profile_advance_writes_answer_to_session(self) -> None:
        _write_lark_operator(self.home, _OPERATOR["open_id"], ["*"])
        session_id = "session_test_write"
        path = _seed_session(
            session_id=session_id,
            profile_id="p1",
            missing_fields=["publisher_account.brand", "search_queries"],
            active_open_id=_OPERATOR["open_id"],
            active_lark_chat_id="oc_test",
        )
        with patch.dict(
            os.environ, {"AGENTFLOW_LARK_APP_PRIMARY": "true"}, clear=False
        ), patch("agentflow.shared.agent_bridge.emit_agent_event"):
            res = lark_callback.handle_event(
                event_kind="card_action",
                article_id=None,
                action="profile_advance",
                payload={
                    "profile_id": "p1",
                    "session_path": str(path),
                    "question_field": "publisher_account.brand",
                    "text": "Acme Corp",
                },
                operator=_OPERATOR,
            )
        self.assertTrue(res["ack"])
        self.assertIn("profile_advance_next_question", res["side_effects"])
        # Session on disk should have collected the answer.
        loaded = lifecycle.load_session(session_id)
        self.assertEqual(
            loaded["collected"]["publisher_account.brand"], "Acme Corp"
        )

    def test_handle_profile_advance_advances_to_next_question(self) -> None:
        _write_lark_operator(self.home, _OPERATOR["open_id"], ["*"])
        session_id = "session_test_advance"
        path = _seed_session(
            session_id=session_id,
            profile_id="p1",
            missing_fields=[
                "publisher_account.brand",
                "publisher_account.voice",
                "search_queries",
            ],
            active_open_id=_OPERATOR["open_id"],
            active_lark_chat_id="oc_test",
        )
        with patch.dict(
            os.environ, {"AGENTFLOW_LARK_APP_PRIMARY": "true"}, clear=False
        ), patch.object(
            review_triggers, "_emit_lark_profile_question_card"
        ) as emit_q:
            res = lark_callback.handle_event(
                event_kind="card_action",
                article_id=None,
                action="profile_advance",
                payload={
                    "profile_id": "p1",
                    "session_path": str(path),
                    "question_field": "publisher_account.brand",
                    "text": "Acme",
                },
                operator=_OPERATOR,
            )
        self.assertIn("profile_advance_next_question", res["side_effects"])
        emit_q.assert_called_once()
        kwargs = emit_q.call_args.kwargs
        # 1st field done, next is publisher_account.voice (index 1) of 3.
        self.assertEqual(kwargs["question_field"], "publisher_account.voice")
        self.assertEqual(kwargs["question_index"], 1)
        self.assertEqual(kwargs["total_questions"], 3)


class HandleProfileAdvanceCompleteTests(_ProfileAdvanceTestCase):
    def test_handle_profile_advance_completes_when_no_more_questions(self) -> None:
        _write_lark_operator(self.home, _OPERATOR["open_id"], ["*"])
        session_id = "session_test_complete"
        path = _seed_session(
            session_id=session_id,
            profile_id="p1",
            missing_fields=["search_queries"],
            active_open_id=_OPERATOR["open_id"],
            active_lark_chat_id="oc_test",
        )
        with patch.dict(
            os.environ, {"AGENTFLOW_LARK_APP_PRIMARY": "true"}, clear=False
        ), patch(
            "agentflow.agent_review.lark_callback.emit_agent_event",
            create=True,
        ) as bridge_mock, patch(
            "agentflow.shared.agent_bridge.emit_agent_event"
        ) as global_bridge_mock, patch.object(
            lifecycle, "release_session_lark", wraps=lifecycle.release_session_lark
        ) as release_mock:
            res = lark_callback.handle_event(
                event_kind="card_action",
                article_id=None,
                action="profile_advance",
                payload={
                    "profile_id": "p1",
                    "session_path": str(path),
                    "question_field": "search_queries",
                    "text": "ai infra; web3 hotspots",
                },
                operator=_OPERATOR,
            )
        self.assertTrue(res["ack"])
        self.assertIn("profile_advance_completed", res["side_effects"])
        # release_session_lark was called with status="completed".
        release_mock.assert_called_once()
        call_args = release_mock.call_args
        self.assertEqual(call_args.args[0], session_id)
        self.assertEqual(call_args.kwargs.get("status"), "completed")
        # notify.profile_setup_done was emitted via agent_bridge module.
        # The handler imports emit_agent_event from agentflow.shared.agent_bridge
        # at call time; the global_bridge_mock catches it.
        emitted_events = [
            c.kwargs.get("event_type")
            for c in global_bridge_mock.call_args_list
        ]
        self.assertIn("notify.profile_setup_done", emitted_events)
        # Card is green (success).
        self.assertEqual(res["reply_card"]["header"]["template"], "green")


# ---------------------------------------------------------------------------
# Auth + validation negative paths
# ---------------------------------------------------------------------------


class HandleProfileAdvanceAuthTests(_ProfileAdvanceTestCase):
    def test_handle_profile_advance_unauthorized_returns_deny(self) -> None:
        # No lark_operators entry → fail-closed deny via _authorize_or_deny_v2.
        session_id = "session_test_unauth"
        path = _seed_session(
            session_id=session_id,
            profile_id="p1",
            missing_fields=["search_queries"],
        )
        res = lark_callback.handle_event(
            event_kind="card_action",
            article_id=None,
            action="profile_advance",
            payload={
                "profile_id": "p1",
                "session_path": str(path),
                "question_field": "search_queries",
                "text": "anything",
            },
            operator=_OPERATOR,
        )
        self.assertTrue(res["ack"])
        self.assertIn("not_authorized", res["side_effects"])
        card = res["reply_card"]
        self.assertIsNotNone(card)
        self.assertEqual(card["header"]["template"], "red")

    def test_handle_profile_advance_empty_answer_returns_deny(self) -> None:
        _write_lark_operator(self.home, _OPERATOR["open_id"], ["*"])
        session_id = "session_test_empty"
        path = _seed_session(
            session_id=session_id,
            profile_id="p1",
            missing_fields=["search_queries"],
            active_open_id=_OPERATOR["open_id"],
        )
        res = lark_callback.handle_event(
            event_kind="card_action",
            article_id=None,
            action="profile_advance",
            payload={
                "profile_id": "p1",
                "session_path": str(path),
                "question_field": "search_queries",
                "text": "   ",  # whitespace-only counts as empty
            },
            operator=_OPERATOR,
        )
        self.assertTrue(res["ack"])
        self.assertIn("missing_answer", res["side_effects"])
        card = res["reply_card"]
        self.assertIsNotNone(card)
        self.assertEqual(card["header"]["template"], "red")


# ---------------------------------------------------------------------------
# Existing-session resume (no re-claim)
# ---------------------------------------------------------------------------


class HandleProfileAdvanceResumeTests(_ProfileAdvanceTestCase):
    def test_handle_profile_advance_resumes_existing_session(self) -> None:
        _write_lark_operator(self.home, _OPERATOR["open_id"], ["*"])
        session_id = "session_test_resume"
        path = _seed_session(
            session_id=session_id,
            profile_id="p1",
            missing_fields=["publisher_account.brand", "search_queries"],
            collected={"publisher_account.brand": "PriorAnswer"},
            active_open_id=_OPERATOR["open_id"],
            active_lark_chat_id="oc_existing",
        )
        # Patch claim_session_lark so we can assert it was NOT called.
        with patch.dict(
            os.environ, {"AGENTFLOW_LARK_APP_PRIMARY": "true"}, clear=False
        ), patch.object(
            lifecycle, "claim_session_lark"
        ) as claim_mock, patch.object(
            review_triggers, "_emit_lark_profile_question_card"
        ):
            res = lark_callback.handle_event(
                event_kind="card_action",
                article_id=None,
                action="profile_advance",
                payload={
                    "profile_id": "p1",
                    "session_path": str(path),
                    "question_field": "search_queries",
                    "text": "topic-a; topic-b",
                },
                operator=_OPERATOR,
            )
        # Existing session matched by find_active_session_lark — no re-claim.
        claim_mock.assert_not_called()
        self.assertTrue(res["ack"])
        self.assertIn("profile_advance_completed", res["side_effects"])
        loaded = lifecycle.load_session(session_id)
        # Both prior + new answers preserved.
        self.assertEqual(
            loaded["collected"]["publisher_account.brand"], "PriorAnswer"
        )
        self.assertEqual(
            loaded["collected"]["search_queries"], "topic-a; topic-b"
        )


# ---------------------------------------------------------------------------
# web.py registration
# ---------------------------------------------------------------------------


class WebLarkProfileAdvanceCommandTests(unittest.TestCase):
    def test_lark_profile_advance_registered(self) -> None:
        commands = review_web._COMMAND_SPECS
        self.assertIn("lark_profile_advance", commands)
        spec = commands["lark_profile_advance"]
        self.assertTrue(spec["in_process"])
        self.assertTrue(spec["dangerous"])
        self.assertEqual(spec["scope"], "review")
        self.assertEqual(spec["timeout_seconds"], 30)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
