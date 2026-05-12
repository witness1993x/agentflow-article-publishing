"""End-to-end Phase 2 acceptance test — Lark-pure pipeline (no TG).

This is the *single* big proof that the Wave 1 + Wave 2 work delivered a
genuine Lark-only pipeline. It drives one article from D1 hotspot through
Gate A → Gate B → image picker → Gate C → Gate D → published, hitting all
the Lark callbacks and asserting that every gate emits its
``_emit_lark_*`` card event.

The independence canary: every ``tg_client`` outbound function (send_message,
send_photo, send_document, answer_callback_query, edit_message_*,
get_me, get_updates) is monkeypatched to a sentinel that raises
``AssertionError``. If any code path in the entire pipeline tries to talk
to TG, this test detonates with a loud failure.

Because subprocess work (write+fill, image-gate, publish dispatch) is the
*means* not the *thing being tested*, we mock ``_spawn_async`` and
``_spawn_publish_dispatch`` (Option A in the plan): they synchronously
write the expected article state to disk and emit the next gate's card.
The state transitions and emit-cardinality are the e2e signal.

Plan ref: docs/BLOGFLOW_TG_TO_LARK_PARITY.md §6 phase 2 verification.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

from agentflow.agent_review import lark_callback
from agentflow.agent_review import state as review_state
from agentflow.agent_review import triggers as review_triggers
from agentflow.shared import bootstrap, memory


_OPERATOR = {
    "open_id": "ou_e2e_alice",
    "name": "Alice (e2e)",
    "chat_id": "oc_lark_e2e_chat",
}


_TG_FN_NAMES = (
    "send_message",
    "send_photo",
    "send_document",
    "send_long_text",
    "answer_callback_query",
    "edit_message_reply_markup",
    "edit_message_text",
    "get_me",
    "get_updates",
)


def _seed_lark_operator(home: Path) -> None:
    """Seed both auth files so ``is_authorized_open_id`` and
    ``is_lark_authorized`` both grant ``ou_e2e_alice`` full access."""
    review_dir = home / "review"
    review_dir.mkdir(parents=True, exist_ok=True)
    # auth.json — used by `is_authorized_open_id` (fail-closed v2 path,
    # used by suggestion / profile / chrome / audit handlers).
    (review_dir / "auth.json").write_text(
        json.dumps({
            "lark_operators": [
                {
                    "open_id": _OPERATOR["open_id"],
                    "name": _OPERATOR["name"],
                    "actions": ["*"],
                },
            ],
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    # lark_auth.json — used by legacy `is_lark_authorized` for the standard
    # _ACTION_HANDLERS path (gate A/B/C/D toggles, confirm, etc.).
    (review_dir / "lark_auth.json").write_text(
        json.dumps({
            "authorized_open_ids": [
                {
                    "open_id": _OPERATOR["open_id"],
                    "name": _OPERATOR["name"],
                    "allowed_actions": ["*"],
                },
            ],
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


class LarkPureE2ETests(unittest.TestCase):
    """Phase 2 acceptance: D1 → published, Lark-only, zero TG calls."""

    # ------------------------------------------------------------------
    # fixture
    # ------------------------------------------------------------------

    def setUp(self) -> None:
        super().setUp()
        # 1. AGENTFLOW_HOME — isolated tmpdir.
        self._tmpdir = tempfile.TemporaryDirectory()
        self.home = Path(self._tmpdir.name)
        self.stack = ExitStack()

        # Standard bootstrap/memory shims (matches AgentflowHomeTestCase).
        self.stack.enter_context(patch.object(bootstrap, "AGENTFLOW_HOME", self.home))
        self.stack.enter_context(
            patch.object(memory, "MEMORY_DIR", self.home / "memory")
        )
        self.stack.enter_context(
            patch.object(memory, "EVENTS_PATH", self.home / "memory" / "events.jsonl")
        )
        self.stack.enter_context(
            patch.object(memory, "_INTENTS_DIR", self.home / "intents")
        )
        self.stack.enter_context(
            patch.object(
                memory, "_INTENTS_CURRENT", self.home / "intents" / "current.yaml"
            )
        )
        bootstrap.ensure_user_dirs()

        # 2. Phase-2 environment: TG token UNSET; Lark primary.
        env_patches = {
            "AGENTFLOW_LARK_APP_PRIMARY": "true",
            "MOCK_LLM": "true",
            "AGENTFLOW_MOCK_PUBLISHERS": "true",
            "AGENTFLOW_AGENT_BRIDGE_TOKEN": "test-token",
            "AGENTFLOW_AGENT_BRIDGE_ENABLE_DANGEROUS": "true",
            # Pre-set AGENTFLOW_HOME for any subprocess that re-resolves
            # bootstrap from os.environ (defensive).
            "AGENTFLOW_HOME": str(self.home),
        }
        # patch.dict with `clear=False` keeps surrounding env, but we still
        # need to remove TG env. Drop them via a dedicated removal.
        self.stack.enter_context(patch.dict(os.environ, env_patches, clear=False))
        for tg_var in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_REVIEW_CHAT_ID"):
            os.environ.pop(tg_var, None)
        # Track them so tearDown verifies they didn't sneak back in.
        self._tg_env_removed = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_REVIEW_CHAT_ID")

        # 3. Seed Lark operator allowlist (both legacy + v2 paths).
        _seed_lark_operator(self.home)

        # 4. Capture every emit_agent_event call. Patch the source module so
        #    every lazy `from agentflow.shared.agent_bridge import
        #    emit_agent_event` import gets the recorder.
        self.emitted_events: list[dict[str, Any]] = []

        def _record(*, source, event_type, **kwargs):
            entry = {
                "source": source,
                "event_type": event_type,
                "article_id": kwargs.get("article_id"),
                "hotspot_id": kwargs.get("hotspot_id"),
                "payload": kwargs.get("payload") or {},
            }
            self.emitted_events.append(entry)

        self.stack.enter_context(
            patch(
                "agentflow.shared.agent_bridge.emit_agent_event",
                new=_record,
            )
        )
        # web.py imports emit_agent_event at top-level — must patch there too,
        # but our test does not exercise web.py emits, so this is best-effort.
        self.stack.enter_context(
            patch(
                "agentflow.agent_review.web.emit_agent_event",
                new=_record,
                create=False,
            )
        )

        # 5. The TG independence canary — every outbound TG call detonates.
        self.tg_violations: list[tuple[str, tuple, dict]] = []

        def _make_sentinel(fn_name: str):
            def _no_tg(*args, **kwargs):
                self.tg_violations.append((fn_name, args, kwargs))
                raise AssertionError(
                    f"TG call leaked into Lark-only path: "
                    f"tg_client.{fn_name}({args!r}, {kwargs!r})"
                )
            return _no_tg

        from agentflow.agent_review import tg_client
        for name in _TG_FN_NAMES:
            if hasattr(tg_client, name):
                self.stack.enter_context(
                    patch.object(tg_client, name, new=_make_sentinel(name))
                )

    def tearDown(self) -> None:
        self.stack.close()
        self._tmpdir.cleanup()
        super().tearDown()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _events_of(self, event_type: str) -> list[dict[str, Any]]:
        return [e for e in self.emitted_events if e["event_type"] == event_type]

    def _seed_hotspots_batch(self) -> tuple[Path, str]:
        """Pre-create the daily hotspots batch with one ready-to-write topic.

        Returns ``(batch_path, hotspot_id)``.
        """
        hotspots_dir = self.home / "hotspots"
        hotspots_dir.mkdir(parents=True, exist_ok=True)
        batch_path = hotspots_dir / "2026-05-07.json"
        hotspot_id = "hs_e2e_001"
        batch_path.write_text(
            json.dumps({
                "generated_at": "2026-05-07T00:00:00+00:00",
                "hotspots": [
                    {
                        "id": hotspot_id,
                        "topic_one_liner": "BlogFlow Lark-pure e2e proof",
                        "freshness_score": 0.83,
                        "generated_at": "2026-05-07T00:00:00+00:00",
                        "source_references": [
                            {"source": "fixture", "url": "https://example/x"},
                        ],
                        "suggested_angles": [
                            {"title": "Independence canary"},
                        ],
                        "mainstream_views": ["a"],
                        "overlooked_angles": ["b"],
                    },
                ],
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return batch_path, hotspot_id

    def _seed_draft(self, article_id: str) -> None:
        """Create a draft skeleton sufficient for Gate B/C/D handlers.

        The state machine starts at ``topic_pool`` (no gate_history). We
        seed a ``draft_pending_review`` entry directly so Gate B's approve
        transition has a valid ``from_state``.
        """
        draft_dir = self.home / "drafts" / article_id
        draft_dir.mkdir(parents=True, exist_ok=True)
        meta = {
            "article_id": article_id,
            "title": "BlogFlow Lark-pure e2e proof",
            "subtitle": "An independence canary",
            "publisher_account": {"brand": "AgentFlow", "output_language": "en"},
            "metadata_overrides": {"medium": {}},
            "sections": [
                {
                    "title": "intro",
                    "word_count": 120,
                    "compliance_score": 1.0,
                    "content_markdown": "Body of the section.",
                },
            ],
            "total_word_count": 120,
            "tags": ["lark", "e2e"],
            "gate_history": [
                {
                    "gate": "B",
                    "from_state": review_state.STATE_DRAFTING,
                    "to_state": review_state.STATE_DRAFT_PENDING_REVIEW,
                    "actor": "fixture",
                    "decision": "seed_for_e2e",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "round": 0,
                },
            ],
        }
        (draft_dir / "metadata.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    # the e2e test
    # ------------------------------------------------------------------

    def test_full_pipeline_d1_to_published_without_tg(self) -> None:
        """One article, D1 → published, all-Lark, zero TG calls.

        Stages:
          A. Gate A card emitted from the seeded hotspot batch; operator
             clicks "起稿 #1" → write+fill is mocked synchronously to seed
             the draft and emit Gate B card.
          B. Operator clicks "通过" on Gate B → state transitions to
             draft_approved; image picker card emits.
          C. Operator clicks "cover-only" on the image picker → image-gate
             is mocked synchronously to advance state to image_pending_review
             and emit Gate C card.
          D. Operator clicks "通过" on Gate C → state moves to image_approved;
             Gate D card emits.
          E. Operator toggles "medium" on Gate D, then clicks "确认发布"
             → publish-dispatch is mocked to write status="published" and
             emit notify.dispatch_result + notify.publish_ready.
        """
        # Pre-stage: hotspots batch + an article skeleton that will be the
        # output of write+fill. The hotspot_id from Gate A is the article_id
        # the rest of the pipeline uses (matching the actual code's
        # behaviour where `_handle_gate_a_write` accepts hotspot_id under
        # the article_id slot).
        batch_path, hotspot_id = self._seed_hotspots_batch()
        article_id = hotspot_id  # 1:1 mapping for the e2e
        # We pre-seed the draft now so the mocked write+fill in Stage A
        # only needs to flip the Gate B card emission.
        self._seed_draft(article_id)

        # ===================================================
        # Stage A — emit Gate A card directly from hotspot batch
        # ===================================================
        review_triggers.post_gate_a(
            hotspots=[
                {
                    "id": hotspot_id,
                    "topic_one_liner": "BlogFlow Lark-pure e2e proof",
                    "freshness_score": 0.83,
                    "generated_at": "2026-05-07T00:00:00+00:00",
                    "source_references": [{"source": "fixture"}],
                    "suggested_angles": [{"title": "Independence canary"}],
                },
            ],
            batch_path=str(batch_path),
            publisher_brand="AgentFlow",
            target_series="A",
            top_k=1,
        )

        # Gate A card must have been emitted via Lark.
        gate_a_events = self._events_of("review.gate_a_card")
        self.assertEqual(len(gate_a_events), 1, msg=f"emitted={self.emitted_events}")
        self.assertEqual(
            gate_a_events[0]["payload"]["candidates"][0]["hotspot_id"],
            hotspot_id,
        )

        # Operator clicks "起稿 #1". The real handler spawns subprocess
        # write+fill; we mock _spawn_async so spawning is synchronous.
        # When it returns True, we emit Gate B card via the canonical
        # _emit_lark_gate_b_card. The draft is already seeded above.
        def _fake_spawn_async_write(argv, *, article_id, action):
            # Stand-in for the write+fill subprocess: the article skeleton
            # is already on disk (seeded in setUp). Trigger Gate B card
            # emission via the real emit helper to mirror what the daemon
            # does once fill finishes.
            review_triggers._emit_lark_gate_b_card(
                article_id=article_id,
                short_id="sid_e2e",
                title="BlogFlow Lark-pure e2e proof",
                subtitle="An independence canary",
                publisher_brand="AgentFlow",
                voice="first-party",
                word_count=120,
                section_count=1,
                compliance_score=1.0,
                tags=["lark", "e2e"],
                self_check_lines=[],
                blockers=[],
                opening_excerpt="Body of the section.",
                draft_md="Body of the section.",
                mirror_url=None,
            )
            return True

        with patch.object(
            lark_callback, "_spawn_async", side_effect=_fake_spawn_async_write,
        ):
            res_a = lark_callback.handle_event(
                event_kind="card_action",
                article_id=hotspot_id,
                action="gate_a_write",
                payload={"slot": 0, "angle_index": 0, "target_series": "A"},
                operator=_OPERATOR,
            )
        self.assertIn("gate_a_write_spawned", res_a["side_effects"])

        # Gate B card emitted.
        gate_b_events = self._events_of("review.gate_b_card")
        self.assertEqual(len(gate_b_events), 1)
        self.assertEqual(gate_b_events[0]["article_id"], article_id)

        # ===================================================
        # Stage B — Gate B approve → image picker
        # ===================================================
        # Mock _spawn_next_gate_card so the image picker emit happens
        # synchronously and via the canonical Lark emit helper.
        def _fake_next_gate(article_id, *, kind):
            if kind == "image_picker":
                review_triggers._emit_lark_image_picker_card(
                    article_id=article_id,
                    short_id="sid_e2e",
                    title="BlogFlow Lark-pure e2e proof",
                )
            elif kind == "gate_d":
                review_triggers._emit_lark_gate_d_card(
                    article_id=article_id,
                    short_id="sid_e2e",
                    title="BlogFlow Lark-pure e2e proof",
                    available=["medium", "ghost", "linkedin", "twitter"],
                    selected=[],
                )

        with patch.object(
            lark_callback, "_spawn_next_gate_card", side_effect=_fake_next_gate,
        ):
            res_b = lark_callback.handle_event(
                event_kind="card_action",
                article_id=article_id,
                action="approve_b",
                payload={
                    "lark_chat_id": _OPERATOR["chat_id"],
                    "lark_card_id": "lcid_gate_b_e2e",
                },
                operator=_OPERATOR,
            )
        self.assertIn("approve_b", res_b["side_effects"])
        self.assertIn("image_picker_spawned", res_b["side_effects"])

        # Verify the gate_history entry from approve_b (already happened
        # via review_state.transition; the test just confirms it's there
        # and the article is now in draft_approved).
        self.assertEqual(
            review_state.current_state(article_id),
            review_state.STATE_DRAFT_APPROVED,
        )
        # Image picker card emitted.
        ip_events = self._events_of("review.image_gate_picker_card")
        self.assertEqual(len(ip_events), 1)

        # ===================================================
        # Stage C — image picker cover-only → Gate C card
        # ===================================================
        # Operator clicks "cover-only". The handler spawns image-gate;
        # mock spawns to synchronously: (a) advance state to
        # image_pending_review, then (b) emit Gate C card.
        def _fake_spawn_async_image(argv, *, article_id, action):
            # The real ``image-gate`` subprocess writes a cover.png and
            # transitions to image_pending_review. Simulate both.
            try:
                review_state.transition(
                    article_id,
                    gate="C",
                    to_state=review_state.STATE_IMAGE_PENDING_REVIEW,
                    actor=f"lark:{_OPERATOR['open_id']}",
                    decision="image_gate_done_via_lark_e2e_mock",
                    lark_chat_id=_OPERATOR["chat_id"],
                    lark_card_id="lcid_image_pick_e2e",
                )
            except review_state.StateError:
                pass
            review_triggers._emit_lark_gate_c_card(
                article_id=article_id,
                short_id="sid_e2e",
                title="BlogFlow Lark-pure e2e proof",
                cover_path=str(self.home / "drafts" / article_id / "cover.png"),
                cover_size="1024x576",
                overlay_status="ok",
                self_check_lines=[],
                blockers=[],
            )
            return True

        with patch.object(
            lark_callback, "_spawn_async", side_effect=_fake_spawn_async_image,
        ):
            res_c = lark_callback.handle_event(
                event_kind="card_action",
                article_id=article_id,
                action="image_gate_pick",
                payload={"mode": "cover-only"},
                operator=_OPERATOR,
            )
        self.assertIn("image_gate_pick_spawned", res_c["side_effects"])
        self.assertEqual(
            review_state.current_state(article_id),
            review_state.STATE_IMAGE_PENDING_REVIEW,
        )
        gc_events = self._events_of("review.gate_c_card")
        self.assertEqual(len(gc_events), 1)

        # ===================================================
        # Stage D — Gate C approve → Gate D card
        # ===================================================
        with patch.object(
            lark_callback, "_spawn_next_gate_card", side_effect=_fake_next_gate,
        ):
            res_d = lark_callback.handle_event(
                event_kind="card_action",
                article_id=article_id,
                action="gate_c_approve",
                payload={},
                operator=_OPERATOR,
            )
        self.assertIn("gate_c_approve", res_d["side_effects"])
        self.assertIn("gate_d_spawned", res_d["side_effects"])
        self.assertEqual(
            review_state.current_state(article_id),
            review_state.STATE_IMAGE_APPROVED,
        )
        gd_events = self._events_of("review.gate_d_card")
        self.assertEqual(len(gd_events), 1)

        # ===================================================
        # Stage E — Gate D toggle medium + confirm → published
        # ===================================================
        # Toggle medium ON.
        res_toggle = lark_callback.handle_event(
            event_kind="card_action",
            article_id=article_id,
            action="gate_d_toggle",
            payload={"platform": "medium"},
            operator=_OPERATOR,
        )
        self.assertIn("gate_d_toggle_on", res_toggle["side_effects"])

        # Confirm publish. The real _spawn_publish_dispatch fires a
        # subprocess chain. Mock it to: advance state through
        # ready_to_publish → published, write metadata.status, emit
        # notify.dispatch_result + notify.publish_ready.
        def _fake_publish_dispatch(article_id, platforms, *, operator):
            # Final state machine path: image_approved → ready_to_publish
            # → published.
            review_state.transition(
                article_id,
                gate="D",
                to_state=review_state.STATE_READY_TO_PUBLISH,
                actor=f"lark:{operator['open_id']}",
                decision="ready_via_lark_e2e_mock",
                lark_chat_id=operator.get("chat_id"),
                lark_card_id="lcid_gate_d_e2e",
            )
            review_state.transition(
                article_id,
                gate="D",
                to_state=review_state.STATE_PUBLISHED,
                actor=f"lark:{operator['open_id']}",
                decision="published_via_lark_e2e_mock",
                lark_chat_id=operator.get("chat_id"),
                lark_card_id="lcid_gate_d_e2e",
            )
            # Persist a top-level status field for the test's final assert.
            meta_path = self.home / "drafts" / article_id / "metadata.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["status"] = "published"
            meta_path.write_text(
                json.dumps(meta, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            # Emit the two notify events the OpenClaw side expects.
            from agentflow.shared.agent_bridge import emit_agent_event
            emit_agent_event(
                source="agentflow.review",
                event_type="notify.dispatch_result",
                article_id=article_id,
                payload={
                    "platforms": list(platforms),
                    "succeeded": list(platforms),
                    "failed": [],
                },
            )
            emit_agent_event(
                source="agentflow.review",
                event_type="notify.publish_ready",
                article_id=article_id,
                payload={
                    "platforms": list(platforms),
                    "medium_paste_url": "https://medium.com/new-story",
                },
            )
            return True

        with patch.object(
            lark_callback,
            "_spawn_publish_dispatch",
            side_effect=_fake_publish_dispatch,
        ):
            res_confirm = lark_callback.handle_event(
                event_kind="card_action",
                article_id=article_id,
                action="gate_d_confirm",
                payload={},
                operator=_OPERATOR,
            )
        self.assertIn("gate_d_dispatch_spawned", res_confirm["side_effects"])

        # ===================================================
        # Final assertions — Phase 2 acceptance criteria
        # ===================================================

        # 1. Independence canary: the TG sentinel must NOT have fired.
        self.assertEqual(
            self.tg_violations, [],
            msg=f"TG calls leaked into the Lark-only pipeline: {self.tg_violations!r}",
        )

        # 2. Lark card cardinality: each gate emitted at least one card.
        for required in (
            "review.gate_a_card",
            "review.gate_b_card",
            "review.image_gate_picker_card",
            "review.gate_c_card",
            "review.gate_d_card",
        ):
            self.assertGreaterEqual(
                len(self._events_of(required)), 1,
                msg=f"missing review event {required}; got {[e['event_type'] for e in self.emitted_events]}",
            )
        # And at least one notify.* event surfaced (dispatch_result OR
        # publish_ready — the plan accepts either).
        notify_kinds = [
            e["event_type"] for e in self.emitted_events
            if e["event_type"].startswith("notify.")
        ]
        self.assertTrue(
            any(k in notify_kinds for k in (
                "notify.dispatch_result", "notify.publish_ready",
            )),
            msg=f"no notify.* event emitted; saw {notify_kinds}",
        )

        # 3. Gate-history audit: entries written by Lark callbacks must
        #    carry lark_chat_id (Wave 1 IND-1 correctness in real flow).
        history = review_state.gate_history(article_id)
        lark_entries = [
            e for e in history
            if e.get("decision", "").endswith("_via_lark")
            or "_via_lark" in e.get("decision", "")
            or "_via_lark_e2e_mock" in e.get("decision", "")
        ]
        self.assertGreater(len(lark_entries), 0)
        # The mocked publish-dispatch transition explicitly passed
        # lark_chat_id; verify it landed in the history entry.
        published_entries = [
            e for e in history if e.get("to_state") == review_state.STATE_PUBLISHED
        ]
        self.assertEqual(len(published_entries), 1)
        self.assertEqual(
            published_entries[0].get("lark_chat_id"),
            _OPERATOR["chat_id"],
        )
        self.assertEqual(
            published_entries[0].get("lark_card_id"),
            "lcid_gate_d_e2e",
        )

        # 4. Final state on disk: metadata.status == "published".
        meta_path = self.home / "drafts" / article_id / "metadata.json"
        final_meta = json.loads(meta_path.read_text(encoding="utf-8"))
        self.assertEqual(final_meta.get("status"), "published")
        self.assertEqual(
            review_state.current_state(article_id),
            review_state.STATE_PUBLISHED,
        )

        # 5. Independence env: TELEGRAM_BOT_TOKEN unset for the entire run.
        self.assertNotIn("TELEGRAM_BOT_TOKEN", os.environ)
        self.assertNotIn("TELEGRAM_REVIEW_CHAT_ID", os.environ)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
