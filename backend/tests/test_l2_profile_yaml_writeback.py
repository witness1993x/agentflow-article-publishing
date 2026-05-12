"""L-2 — Lark profile completion writes back to ``topic_profiles.yaml``.

Plan ref: docs/BLOGFLOW_TG_TO_LARK_PARITY.md §11.5 L-2.

The completion branch of ``_handle_profile_advance`` must mutate
``~/.agentflow/topic_profiles.yaml`` so the Lark profile setup flow has
the same end-to-end semantics as TG. Failures in writeback must be
non-fatal: the session still releases, ``notify.profile_setup_done`` still
emits, and the success card body surfaces the warning.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import yaml

from agentflow.agent_review import lark_callback
from agentflow.config.topic_profiles_loader import user_topic_profiles_path
from agentflow.shared import bootstrap, memory
from agentflow.shared import topic_profile_lifecycle as lifecycle


_OPERATOR = {"open_id": "ou_op_test", "name": "Tester"}


def _write_lark_operator(home: Path, open_id: str, actions: list[str]) -> None:
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


def _read_profile_yaml() -> dict:
    """Read the on-disk topic_profiles.yaml (post-AGENTFLOW_HOME patch)."""
    path = user_topic_profiles_path()
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


class _WritebackTestCase(unittest.TestCase):
    """Tmp AGENTFLOW_HOME + memory redirect + auth seeded."""

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


class CompletionWritesBrandTests(_WritebackTestCase):
    def test_completion_writes_brand_to_profile_yaml(self) -> None:
        session_id = "session_l2_brand"
        path = _seed_session(
            session_id=session_id,
            profile_id="p_l2_brand",
            missing_fields=["publisher_account.brand"],
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
                    "profile_id": "p_l2_brand",
                    "session_path": str(path),
                    "question_field": "publisher_account.brand",
                    "text": "Acme Corp",
                },
                operator=_OPERATOR,
            )
        self.assertTrue(res["ack"])
        self.assertIn("profile_advance_completed", res["side_effects"])
        self.assertIn("profile_yaml_written", res["side_effects"])

        data = _read_profile_yaml()
        profiles = data.get("profiles") or {}
        self.assertIn("p_l2_brand", profiles)
        self.assertEqual(
            profiles["p_l2_brand"]["publisher_account"]["brand"], "Acme Corp"
        )
        self.assertEqual(res["reply_card"]["header"]["template"], "green")


class CompletionListFieldTests(_WritebackTestCase):
    def test_completion_writes_list_field_with_comma_separator(self) -> None:
        session_id = "session_l2_do"
        path = _seed_session(
            session_id=session_id,
            profile_id="p_l2_do",
            missing_fields=["publisher_account.do"],
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
                    "profile_id": "p_l2_do",
                    "session_path": str(path),
                    "question_field": "publisher_account.do",
                    "text": "短句, 数据点, 不卖",
                },
                operator=_OPERATOR,
            )
        self.assertIn("profile_yaml_written", res["side_effects"])
        data = _read_profile_yaml()
        do_list = data["profiles"]["p_l2_do"]["publisher_account"]["do"]
        self.assertEqual(do_list, ["短句", "数据点", "不卖"])


class CompletionPartialFailureTests(_WritebackTestCase):
    def test_completion_partial_failure_releases_session_anyway(self) -> None:
        session_id = "session_l2_failwrite"
        path = _seed_session(
            session_id=session_id,
            profile_id="p_l2_fail",
            missing_fields=["publisher_account.brand"],
            active_open_id=_OPERATOR["open_id"],
            active_lark_chat_id="oc_test",
        )

        # upsert_profile is imported lazily inside _handle_profile_advance —
        # patching the lifecycle-module symbol intercepts the lazy import.
        with patch.dict(
            os.environ, {"AGENTFLOW_LARK_APP_PRIMARY": "true"}, clear=False
        ), patch.object(
            lifecycle, "upsert_profile", side_effect=RuntimeError("disk full")
        ), patch.object(
            lifecycle, "release_session_lark", wraps=lifecycle.release_session_lark
        ) as release_mock, patch(
            "agentflow.shared.agent_bridge.emit_agent_event"
        ) as bridge_mock:
            res = lark_callback.handle_event(
                event_kind="card_action",
                article_id=None,
                action="profile_advance",
                payload={
                    "profile_id": "p_l2_fail",
                    "session_path": str(path),
                    "question_field": "publisher_account.brand",
                    "text": "Acme",
                },
                operator=_OPERATOR,
            )
        # Session still released with status="completed".
        release_mock.assert_called_once()
        self.assertEqual(release_mock.call_args.args[0], session_id)
        self.assertEqual(release_mock.call_args.kwargs.get("status"), "completed")
        # notify.profile_setup_done still emitted.
        emitted = [
            c.kwargs.get("event_type") for c in bridge_mock.call_args_list
        ]
        self.assertIn("notify.profile_setup_done", emitted)
        # Side effect signals failure.
        self.assertIn("profile_yaml_writeback_failed", res["side_effects"])
        self.assertNotIn("profile_yaml_written", res["side_effects"])
        # Card body has the warning line.
        body = res["reply_card"]["elements"][0]["text"]["content"]
        self.assertIn("写回失败", body)
        self.assertIn("disk full", body)


class CompletionEmitNotifyTests(_WritebackTestCase):
    def test_completion_emits_notify_with_completed_fields(self) -> None:
        session_id = "session_l2_emit"
        path = _seed_session(
            session_id=session_id,
            profile_id="p_l2_emit",
            missing_fields=["publisher_account.brand"],
            collected={"search_queries": "alpha; beta"},
            active_open_id=_OPERATOR["open_id"],
            active_lark_chat_id="oc_test",
        )
        with patch.dict(
            os.environ, {"AGENTFLOW_LARK_APP_PRIMARY": "true"}, clear=False
        ), patch(
            "agentflow.shared.agent_bridge.emit_agent_event"
        ) as bridge_mock:
            res = lark_callback.handle_event(
                event_kind="card_action",
                article_id=None,
                action="profile_advance",
                payload={
                    "profile_id": "p_l2_emit",
                    "session_path": str(path),
                    "question_field": "publisher_account.brand",
                    "text": "Acme",
                },
                operator=_OPERATOR,
            )
        self.assertIn("profile_advance_completed", res["side_effects"])
        # Find the notify call.
        notify_calls = [
            c
            for c in bridge_mock.call_args_list
            if c.kwargs.get("event_type") == "notify.profile_setup_done"
        ]
        self.assertEqual(len(notify_calls), 1)
        payload = notify_calls[0].kwargs.get("payload") or {}
        self.assertEqual(payload["profile_id"], "p_l2_emit")
        self.assertEqual(payload["next_action"], "d1_scan")
        # Both prior + new collected keys appear.
        self.assertIn("publisher_account.brand", payload["completed_fields"])
        self.assertIn("search_queries", payload["completed_fields"])

        # Yaml on disk should reflect the answers (brand + parsed search_queries).
        data = _read_profile_yaml()
        prof = data["profiles"]["p_l2_emit"]
        self.assertEqual(prof["publisher_account"]["brand"], "Acme")
        # search_queries split on semicolon into a list.
        self.assertEqual(prof["search_queries"], ["alpha", "beta"])


class CompletionUnknownFieldTests(_WritebackTestCase):
    def test_completion_with_unknown_field_passes_through(self) -> None:
        # collected has a field that's NOT in _PROFILE_FIELD_TO_SLOT.
        # Translation passes it through; build_patch_from_answers ignores
        # unknown keys gracefully (it only reads named slots). Writeback
        # should still succeed for the known sibling field.
        session_id = "session_l2_unknown"
        path = _seed_session(
            session_id=session_id,
            profile_id="p_l2_unknown",
            missing_fields=["publisher_account.brand"],
            collected={"some.exotic.future_field": "ignored_value"},
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
                    "profile_id": "p_l2_unknown",
                    "session_path": str(path),
                    "question_field": "publisher_account.brand",
                    "text": "Acme",
                },
                operator=_OPERATOR,
            )
        # Writeback succeeded — unknown field was passthrough, no crash.
        self.assertIn("profile_yaml_written", res["side_effects"])
        self.assertNotIn("profile_yaml_writeback_failed", res["side_effects"])
        data = _read_profile_yaml()
        prof = data["profiles"]["p_l2_unknown"]
        # Brand was applied.
        self.assertEqual(prof["publisher_account"]["brand"], "Acme")


# ---------------------------------------------------------------------------
# Translation helper unit tests (pure logic, no I/O)
# ---------------------------------------------------------------------------


class TranslationHelperTests(unittest.TestCase):
    def test_collected_to_slot_dict_maps_known_dotted_keys(self) -> None:
        translated = lark_callback._collected_to_slot_dict(
            {
                "publisher_account.brand": "Acme",
                "publisher_account.voice": "first_party_brand",
                "search_queries": "a; b",
                "keyword_groups.core": "x, y",
            }
        )
        self.assertEqual(translated["brand"], "Acme")
        self.assertEqual(translated["voice"], "first_party_brand")
        self.assertEqual(translated["search_queries"], ["a", "b"])
        self.assertEqual(translated["core_terms"], ["x", "y"])

    def test_collected_to_slot_dict_passthrough_unknown_keys(self) -> None:
        translated = lark_callback._collected_to_slot_dict(
            {"unknown.field": "v"}
        )
        self.assertEqual(translated["unknown.field"], "v")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
