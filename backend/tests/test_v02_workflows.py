from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner
from fastapi.testclient import TestClient

from agentflow.agent_d2 import image_generator
from agentflow.agent_review import render as review_render
from agentflow.agent_review import state as review_state
from agentflow.agent_review import daemon as review_daemon
from agentflow.agent_review import short_id as review_short_id
from agentflow.agent_review.web import create_app
from agentflow.agent_d2.main import save_draft
from agentflow.agent_d4 import storage as d4_storage
from agentflow.agent_email import storage as email_storage
from agentflow.cli.commands import cli
from agentflow.shared import bootstrap, memory, preferences
from agentflow.shared.llm_client import LLMClient
from agentflow.shared.topic_profile_lifecycle import save_session
from agentflow.shared.topic_profile_lifecycle import save_suggestion
from agentflow.shared.models import (
    D1Output,
    D3Output,
    DraftOutput,
    FilledSection,
    Hotspot,
    ImagePlaceholder,
    OpeningCandidate,
    PlatformVersion,
    PublishResult,
    Section,
    SkeletonOutput,
    SuggestedAngle,
    TitleCandidate,
    ClosingCandidate,
)


def _parse_json_output(output: str) -> dict:
    start = output.find("{")
    if start < 0:
        raise AssertionError(f"no JSON object found in output: {output!r}")
    return json.loads(output[start:])


class AgentflowHomeTestCase(unittest.TestCase):
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
            patch.object(memory, "_INTENTS_DIR", self.home / "intents")
        )
        self.stack.enter_context(
            patch.object(
                memory, "_INTENTS_CURRENT", self.home / "intents" / "current.yaml"
            )
        )
        self.stack.enter_context(
            patch.object(preferences, "DEFAULT_PREFS_PATH", self.home / "preferences.yaml")
        )
        self.stack.enter_context(
            patch.object(d4_storage, "HISTORY_PATH", self.home / "publish_history.jsonl")
        )
        bootstrap.ensure_user_dirs()

    def tearDown(self) -> None:
        self.stack.close()
        self._tmpdir.cleanup()
        super().tearDown()


class LearningReviewTests(AgentflowHomeTestCase):
    def test_learning_review_json_counts_and_recommendations(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        save_suggestion(
            {
                "id": "suggestion_pending",
                "profile_id": "chainstream",
                "stage": "search",
                "title": "Promote query",
                "summary": "Add a reusable query.",
                "status": "pending",
                "created_at": now,
            }
        )
        save_suggestion(
            {
                "id": "suggestion_applied",
                "profile_id": "chainstream",
                "stage": "publish",
                "title": "Promote tag",
                "summary": "Add a default tag.",
                "status": "applied",
                "created_at": now,
                "applied_at": now,
            }
        )
        save_suggestion(
            {
                "id": "suggestion_dismissed",
                "profile_id": "chainstream",
                "stage": "hotspots",
                "title": "Ignore noisy recall",
                "summary": "Dismiss noisy evidence.",
                "status": "dismissed",
                "created_at": now,
                "updated_at": now,
            }
        )

        history_path = self.home / "publish_history.jsonl"
        history_records = [
            {
                "article_id": "art_success",
                "platform": "ghost_wordpress",
                "status": "success",
                "published_url": "https://example.test/art-success",
                "published_at": now,
                "failure_reason": None,
            },
            {
                "article_id": "art_failed",
                "platform": "medium",
                "status": "failed",
                "published_url": None,
                "published_at": now,
                "failure_reason": "missing token",
            },
            {
                "article_id": "art_success",
                "platform": "ghost_wordpress",
                "status": "rolled_back",
                "published_url": "https://example.test/art-success",
                "published_at": now,
                "failure_reason": None,
            },
        ]
        history_path.write_text(
            "\n".join(json.dumps(record) for record in history_records) + "\n",
            encoding="utf-8",
        )

        for event_type in (
            "topic_profile_suggestion_created",
            "topic_profile_suggestion_applied",
            "topic_intent_used",
            "article_created",
            "fill_choices",
            "fill_choices",
            "fill_choices",
        ):
            memory.append_memory_event(event_type, article_id="art_success")

        style_sample = self.home / "style_corpus" / "sample.md"
        style_sample.write_text("# Sample\n\nPublished style sample.", encoding="utf-8")

        runner = CliRunner()
        result = runner.invoke(cli, ["learning-review", "--since", "7d", "--json"])
        self.assertEqual(result.exit_code, 0, result.output)
        payload = _parse_json_output(result.output)

        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["suggestions"]["counts"]["pending"], 1)
        self.assertEqual(payload["suggestions"]["counts"]["applied"], 1)
        self.assertEqual(payload["suggestions"]["counts"]["dismissed"], 1)
        self.assertEqual(payload["publish_history"]["counts"]["success"], 1)
        self.assertEqual(payload["publish_history"]["counts"]["failed"], 1)
        self.assertEqual(payload["publish_history"]["counts"]["rolled_back"], 1)
        self.assertEqual(
            payload["publish_history"]["per_platform"]["ghost_wordpress"]["success"],
            1,
        )
        self.assertEqual(
            payload["memory_events"]["counts"]["topic_profile_suggestion_created"],
            1,
        )
        self.assertEqual(payload["memory_events"]["counts"]["fill_choices"], 3)
        self.assertEqual(payload["style_learning"]["style_corpus_count"], 1)
        self.assertFalse(payload["style_learning"]["style_profile_exists"])
        self.assertTrue(
            payload["style_learning"]["recommend_learn_style_from_published"]
        )
        self.assertTrue(
            any("learn-style --from-published" in rec for rec in payload["recommendations"])
        )
        self.assertTrue(any("pending constraint" in rec for rec in payload["recommendations"]))


class TopicProfileIntentTests(AgentflowHomeTestCase):
    def setUp(self) -> None:
        super().setUp()
        # v1.0.7 made config-examples/topic_profiles.example.yaml brand-neutral
        # (no more chainstream). These tests use chainstream as their own
        # fixture, so seed it into the test's AGENTFLOW_HOME directly rather
        # than relying on the now-neutral example file.
        topic_profiles = {
            "version": "1.0",
            "profiles": {
                "chainstream": {
                    "label": "ChainStream",
                    "summary": "AI-native crypto infrastructure for real-time on-chain data and AI execution.",
                    "intent": "AI-native crypto infra fixture for tests.",
                    "keyword_groups": {
                        "core": [
                            "AI-native crypto infrastructure",
                            "real-time on-chain data",
                            "Kafka Streams",
                        ],
                        "ai_execution": ["MCP", "AI execution", "AI agent"],
                        "ecosystem": ["Ethereum", "Solana", "BSC"],
                    },
                    "hotspot_terms": [
                        "AI-native crypto infrastructure",
                        "real-time on-chain data",
                        "Kafka Streams",
                        "MCP",
                    ],
                    "search_queries": [
                        "Kafka Streams",
                        "real-time on-chain data",
                        "web3 data infrastructure",
                        "MCP",
                    ],
                    "default_search_query": "Kafka Streams",
                    "avoid_terms": ["celebrity crypto", "macro politics"],
                    "publisher_account": {
                        "brand": "ChainStream",
                        "output_language": "zh-Hans",
                    },
                },
            },
        }
        import yaml as _yaml
        (self.home / "topic_profiles.yaml").write_text(
            _yaml.safe_dump(topic_profiles, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

    def test_stale_session_intent_expires_before_shadowing_default_profile(self) -> None:
        stale_intent = {
            "schema_version": 1,
            "created_at": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
            "source": "cli_flag",
            "query": {"text": "old chainstream query", "mode": "keyword"},
            "metadata": {"ttl": "session"},
            "profile": {"id": "chainstream", "label": "ChainStream"},
        }
        intent_path = self.home / "intents" / "current.yaml"
        intent_path.parent.mkdir(parents=True, exist_ok=True)
        intent_path.write_text(json.dumps(stale_intent), encoding="utf-8")

        with patch.dict(
            os.environ,
            {"AGENTFLOW_SESSION_INTENT_MAX_HOURS": "12"},
            clear=False,
        ):
            self.assertIsNone(memory.load_current_intent())

        self.assertFalse(intent_path.exists())

    def test_intent_set_from_profile_persists_expanded_keywords(self) -> None:
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "intent-set",
                "--profile",
                "chainstream",
                "--ttl",
                "persistent",
                "--json",
            ],
        )
        self.assertEqual(result.exit_code, 0, result.output)
        payload = _parse_json_output(result.output)
        intent = payload["intent"]
        self.assertEqual(intent["profile"]["id"], "chainstream")
        self.assertIn("Kafka Streams", intent["keywords"]["expanded"])

        show = runner.invoke(cli, ["intent-show", "--json"])
        self.assertEqual(show.exit_code, 0, show.output)
        shown = _parse_json_output(show.output)
        self.assertEqual(shown["intent"]["profile"]["id"], "chainstream")

    def test_hotspots_profile_intent_uses_hybrid_recall_and_soft_rerank(self) -> None:
        runner = CliRunner()
        set_result = runner.invoke(cli, ["intent-set", "--profile", "chainstream"])
        self.assertEqual(set_result.exit_code, 0, set_result.output)

        output = D1Output(
            generated_at=datetime.now(timezone.utc),
            hotspots=[
                Hotspot(
                    id="hs_chainstream",
                    topic_one_liner="Kafka Streams for multi-chain on-chain data",
                    source_references=[{"text_snippet": "MCP plus token-prices stream"}],
                    mainstream_views=[],
                    overlooked_angles=[],
                    recommended_series="C",
                    series_confidence=0.9,
                    suggested_angles=[
                        SuggestedAngle(
                            angle="Why Kafka beats WebSocket for server-side consumers",
                            fit_explanation="Infra comparison",
                            depth="medium",
                            difficulty="medium",
                        )
                    ],
                    freshness_score=0.8,
                    depth_potential="high",
                    generated_at=datetime.now(timezone.utc),
                ),
                Hotspot(
                    id="hs_other",
                    topic_one_liner="General AI image model release",
                    source_references=[{"text_snippet": "consumer image editing feature"}],
                    mainstream_views=[],
                    overlooked_angles=[],
                    recommended_series="A",
                    series_confidence=0.4,
                    suggested_angles=[
                        SuggestedAngle(
                            angle="Product launch recap",
                            fit_explanation="General topic",
                            depth="low",
                            difficulty="low",
                        )
                    ],
                    freshness_score=0.3,
                    depth_potential="low",
                    generated_at=datetime.now(timezone.utc),
                ),
            ],
        )

        seen_queries: list[str] = []

        def _fake_search(query: str, days: int, min_points: int, target_candidates: int):
            seen_queries.append(query)
            search_output = D1Output(
                generated_at=datetime.now(timezone.utc),
                hotspots=[
                    Hotspot(
                        id=f"sr_chainstream_{len(seen_queries)}",
                        topic_one_liner=f"{query} search recall for ChainStream",
                        source_references=[{"text_snippet": f"{query} search hit"}],
                        mainstream_views=[],
                        overlooked_angles=[],
                        recommended_series="C",
                        series_confidence=0.8,
                        suggested_angles=[
                            SuggestedAngle(
                                angle="Profile search bundle angle",
                                fit_explanation="hybrid recall",
                                depth="medium",
                                difficulty="medium",
                            )
                        ],
                        freshness_score=0.6,
                        depth_potential="medium",
                        generated_at=datetime.now(timezone.utc),
                    )
                ],
            )
            return search_output, self.home / "search_results" / f"{query}.json"

        with (
            patch.dict(os.environ, {"MOCK_LLM": "true"}, clear=False),
            patch("agentflow.agent_d1.main.run_d1_scan", return_value=output),
            patch("agentflow.agent_d1.search.run_d1_search", side_effect=_fake_search),
        ):
            result = runner.invoke(cli, ["hotspots", "--json"])

        self.assertEqual(result.exit_code, 0, result.output)
        payload = _parse_json_output(result.output)
        self.assertGreaterEqual(len(seen_queries), 3)
        self.assertGreater(len(payload["hotspots"]), 2)
        self.assertIn("recall", payload)
        self.assertIn("rerank", payload)
        self.assertEqual(payload["recall"]["scan_count"], 2)
        self.assertEqual(payload["recall"]["search_count"], len(seen_queries))
        self.assertGreater(payload["recall"]["merged_count"], payload["recall"]["scan_count"])
        self.assertEqual(payload["recall"]["kept_count"], len(payload["hotspots"]))
        self.assertEqual(payload["recall"]["strategy"], "scan_plus_profile_search_bundle")
        self.assertEqual(payload["rerank"]["strategy"], "topic_fit_freshness_regex_hint")
        self.assertTrue(payload["rerank"]["topic_fit_preview"])
        self.assertIn("filter", payload)
        self.assertGreaterEqual(payload["filter"]["matched"], 1)
        self.assertEqual(payload["filter"]["total"], payload["recall"]["merged_count"])
        self.assertEqual(payload["filter"]["mode"], "soft_rerank")
        self.assertTrue(payload["filter"]["filtered_out_preview"])

    def test_gate_a_post_is_idempotent_for_active_batch_card(self) -> None:
        from agentflow.agent_review import triggers

        batch_path = str(self.home / "hotspots" / "2026-04-29.json")
        hotspot = {
            "id": "hs_uniswap_001",
            "topic_one_liner": "Uniswap routing policy update",
            "freshness_score": 0.9,
            "suggested_angles": [{"title": "What changed for LPs"}],
            "source_references": [{"source": "hn"}],
        }

        with (
            patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-token"}, clear=False),
            patch("agentflow.agent_review.daemon.get_review_chat_id", return_value=123),
            patch(
                "agentflow.agent_review.tg_client.send_message",
                return_value={"message_id": 456},
            ) as send_mock,
        ):
            first = triggers.post_gate_a(
                hotspots=[hotspot],
                batch_path=batch_path,
                publisher_brand="Uniswap",
            )
            second = triggers.post_gate_a(
                hotspots=[hotspot],
                batch_path=batch_path,
                publisher_brand="Uniswap",
            )

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        assert first is not None and second is not None
        self.assertFalse(first.get("duplicate", False))
        self.assertTrue(second.get("duplicate"))
        self.assertEqual(second["short_id"], first["short_id"])
        send_mock.assert_called_once()

    def test_search_profile_runs_all_configured_queries(self) -> None:
        runner = CliRunner()
        seen_queries: list[str] = []

        def _fake_search(query: str, days: int, min_points: int, target_candidates: int):
            seen_queries.append(query)
            output = D1Output(
                generated_at=datetime.now(timezone.utc),
                hotspots=[
                    Hotspot(
                        id=f"hs_{len(seen_queries)}",
                        topic_one_liner=f"{query} topic",
                        source_references=[{"text_snippet": query}],
                        mainstream_views=[],
                        overlooked_angles=[],
                        recommended_series="C",
                        series_confidence=0.8,
                        suggested_angles=[
                            SuggestedAngle(
                                angle=f"{query} angle",
                                fit_explanation="profile search",
                                depth="medium",
                                difficulty="medium",
                            )
                        ],
                        freshness_score=0.5,
                        depth_potential="medium",
                        generated_at=datetime.now(timezone.utc),
                    )
                ],
            )
            return output, self.home / "hotspots" / f"{query}.json"

        with patch("agentflow.agent_d1.search.run_d1_search", side_effect=_fake_search):
            result = runner.invoke(
                cli,
                ["search", "--profile", "chainstream", "--json"],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = _parse_json_output(result.output)
        self.assertGreaterEqual(len(seen_queries), 3)
        self.assertIn("Kafka Streams", seen_queries)
        self.assertIn("real-time on-chain data", seen_queries)
        self.assertIn("web3 data infrastructure", seen_queries)
        self.assertEqual(len(payload["hotspots"]), len(seen_queries))

    def test_search_writes_separate_archive_with_trace_context(self) -> None:
        from agentflow.agent_d1.search import run_d1_search
        from agentflow.shared.models import RawSignal, TopicCluster

        signal = RawSignal(
            source="hackernews_search",
            source_item_id="hn_1",
            author="hn",
            text="AI agent deleted the production database",
            url="https://example.com/post",
            published_at=datetime.now(timezone.utc),
            engagement={"score": 100},
        )
        hotspot = Hotspot(
            id="hs_old",
            topic_one_liner="AI agent production incident",
            source_references=[{"text_snippet": "incident"}],
            mainstream_views=[],
            overlooked_angles=[],
            recommended_series="A",
            series_confidence=0.9,
            suggested_angles=[
                SuggestedAngle(
                    angle="Traceability matters",
                    fit_explanation="good fit",
                    depth="medium",
                    difficulty="medium",
                )
            ],
            freshness_score=0.7,
            depth_potential="medium",
            generated_at=datetime.now(timezone.utc),
        )
        cluster = TopicCluster(
            cluster_id="cluster_1",
            signals=[signal],
            centroid_embedding=[0.1, 0.2],
            summary_one_liner="incident cluster",
        )

        async def _fake_search(**_: object):
            return [signal]

        async def _fake_cluster(signals: list[RawSignal]):
            return [cluster] if signals else []

        async def _fake_mine(*_: object, **__: object):
            return hotspot

        with (
            patch("agentflow.agent_d1.search.hn_algolia.search", side_effect=_fake_search),
            patch("agentflow.agent_d1.search.clustering.cluster", side_effect=_fake_cluster),
            patch("agentflow.agent_d1.search.scoring.select_top", return_value=[cluster]),
            patch("agentflow.agent_d1.search.viewpoint_miner.mine", side_effect=_fake_mine),
            patch("agentflow.agent_d1.search.load_style_profile", return_value={}),
            patch("agentflow.agent_d1.search._load_content_matrix", return_value={}),
        ):
            output, saved_path = asyncio.run(
                run_d1_search("AI agent production database", days=7, min_points=10)
            )

        self.assertEqual(saved_path.parent, self.home / "search_results")
        self.assertTrue(output.hotspots)
        self.assertTrue(output.hotspots[0].id.startswith("sr_ai_agent_production_"))
        saved_payload = json.loads(saved_path.read_text(encoding="utf-8"))
        self.assertEqual(saved_payload["kind"], "search_result")
        self.assertEqual(
            saved_payload["search_context"]["query"], "AI agent production database"
        )

    def test_write_finds_hotspot_in_search_results_archive(self) -> None:
        runner = CliRunner()
        hotspot_id = "sr_trace_test_20260427_001"
        search_path = self.home / "search_results" / "search_trace_test_20260427120000.json"
        search_path.write_text(
            json.dumps(
                {
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "kind": "search_result",
                    "search_context": {"query": "trace test"},
                    "hotspots": [
                        {
                            "id": hotspot_id,
                            "topic_one_liner": "Traceable search hotspot",
                            "source_references": [{"text_snippet": "trace me"}],
                            "mainstream_views": [],
                            "overlooked_angles": [],
                            "recommended_series": "A",
                            "series_confidence": 0.8,
                            "suggested_angles": [
                                {
                                    "angle": "Traceability angle",
                                    "fit_explanation": "fit",
                                    "depth": "medium",
                                    "difficulty": "medium",
                                }
                            ],
                            "freshness_score": 0.6,
                            "depth_potential": "medium",
                            "generated_at": datetime.now(timezone.utc).isoformat(),
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        async def _fake_generate(*_: object, **__: object):
            return SkeletonOutput(
                title_candidates=[
                    TitleCandidate(text="Trace title", style="declarative", rationale="r")
                ],
                opening_candidates=[
                    OpeningCandidate(
                        opening_text="Trace opening", style="data", hook_strength="strong"
                    )
                ],
                section_outline=[
                    Section(
                        heading="Section 1",
                        key_arguments=["arg"],
                        estimated_words=200,
                        section_purpose="purpose",
                    )
                ],
                closing_candidates=[
                    ClosingCandidate(closing_text="Trace closing", style="cta")
                ],
            )

        with patch(
            "agentflow.agent_d2.main.generate_skeleton_for_hotspot",
            side_effect=_fake_generate,
        ):
            result = runner.invoke(cli, ["write", hotspot_id, "--json"])

        self.assertEqual(result.exit_code, 0, result.output)
        payload = _parse_json_output(result.output)
        self.assertEqual(payload["hotspot_id"], hotspot_id)

    def test_topic_profile_init_from_session_materializes_user_file(self) -> None:
        runner = CliRunner()
        session_id = "session_profile_init"
        save_session(
            {
                "id": session_id,
                "profile_id": "chainstream",
                "mode": "init",
                "status": "completed",
                "answers": {
                    "brand": "ChainStream",
                    "voice": "first_party_brand",
                    "product_facts": ["AI-native crypto infra"],
                    "do": ["用我们开口"],
                    "dont": ["不要第三方口吻"],
                    "core_terms": ["on-chain data", "Kafka Streams"],
                    "search_queries": ["Kafka Streams", "on-chain data"],
                    "avoid_terms": ["general AI hype"],
                },
            }
        )

        with patch("agentflow.cli.topic_profile_commands.emit_agent_event") as emit_mock:
            result = runner.invoke(
                cli,
                [
                    "topic-profile",
                    "init",
                    "--profile",
                    "chainstream",
                    "--from-session",
                    session_id,
                    "--json",
                ],
            )
        self.assertEqual(result.exit_code, 0, result.output)
        emit_mock.assert_called_once()
        event_kwargs = emit_mock.call_args.kwargs
        self.assertEqual(event_kwargs["event_type"], "profile.updated")
        self.assertEqual(event_kwargs["payload"]["profile_id"], "chainstream")
        self.assertEqual(event_kwargs["payload"]["mode"], "init")
        self.assertEqual(event_kwargs["payload"]["source"], f"session:{session_id}")
        payload = _parse_json_output(result.output)
        profile = payload["profile"]
        self.assertEqual(profile["publisher_account"]["brand"], "ChainStream")
        self.assertEqual(profile["publisher_account"]["output_language"], "zh-Hans")
        self.assertIn("Kafka Streams", profile["search_queries"])
        self.assertTrue((self.home / "topic_profiles.yaml").exists())

        update_file = self.home / "profile_update.yaml"
        update_file.write_text(
            json.dumps({"search_queries": ["Kafka Streams", "stream processing"]}),
            encoding="utf-8",
        )
        with patch("agentflow.cli.topic_profile_commands.emit_agent_event") as emit_mock:
            update_result = runner.invoke(
                cli,
                [
                    "topic-profile",
                    "update",
                    "--profile",
                    "chainstream",
                    "--from-file",
                    str(update_file),
                    "--json",
                ],
            )
        self.assertEqual(update_result.exit_code, 0, update_result.output)
        emit_mock.assert_called_once()
        event_kwargs = emit_mock.call_args.kwargs
        self.assertEqual(event_kwargs["event_type"], "profile.updated")
        self.assertEqual(event_kwargs["payload"]["profile_id"], "chainstream")
        self.assertEqual(event_kwargs["payload"]["mode"], "update")
        self.assertEqual(event_kwargs["payload"]["source"], f"file:{update_file}")

    def test_topic_profile_apply_merges_suggestion(self) -> None:
        runner = CliRunner()
        session_id = "session_profile_base"
        save_session(
            {
                "id": session_id,
                "profile_id": "chainstream",
                "mode": "init",
                "status": "completed",
                "answers": {
                    "brand": "ChainStream",
                    "voice": "first_party_brand",
                    "product_facts": ["AI-native crypto infra"],
                    "do": ["用我们开口"],
                    "dont": ["不要第三方口吻"],
                    "core_terms": ["on-chain data"],
                    "search_queries": ["on-chain data"],
                    "avoid_terms": ["general AI hype"],
                },
            }
        )
        runner.invoke(
            cli,
            [
                "topic-profile",
                "init",
                "--profile",
                "chainstream",
                "--from-session",
                session_id,
            ],
        )

        suggestion_file = self.home / "patch.json"
        suggestion_file.write_text(
            json.dumps(
                {
                    "proposed_patch": {
                        "search_queries": ["on-chain data", "smart money"],
                    },
                    "evidence": [{"stage": "search"}],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        suggest_result = runner.invoke(
            cli,
            [
                "topic-profile",
                "suggest",
                "--profile",
                "chainstream",
                "--stage",
                "search",
                "--title",
                "Search query expansion suggestion",
                "--summary",
                "expand search queries",
                "--from-file",
                str(suggestion_file),
                "--json",
            ],
        )
        self.assertEqual(suggest_result.exit_code, 0, suggest_result.output)
        suggestion_payload = _parse_json_output(suggest_result.output)
        with patch("agentflow.cli.topic_profile_commands.emit_agent_event") as emit_mock:
            apply_result = runner.invoke(
                cli,
                ["topic-profile", "apply", suggestion_payload["id"], "--json"],
            )
        self.assertEqual(apply_result.exit_code, 0, apply_result.output)
        emit_mock.assert_called_once()
        event_kwargs = emit_mock.call_args.kwargs
        self.assertEqual(event_kwargs["event_type"], "profile.suggestion_applied")
        self.assertEqual(event_kwargs["payload"]["profile_id"], "chainstream")
        self.assertEqual(event_kwargs["payload"]["mode"], "apply")
        self.assertEqual(event_kwargs["payload"]["suggestion_id"], suggestion_payload["id"])
        apply_payload = _parse_json_output(apply_result.output)
        self.assertIn(
            "smart money",
            apply_payload["applied"]["profile"]["search_queries"],
        )

        list_result = runner.invoke(
            cli,
            ["topic-profile", "suggestion-list", "--profile", "chainstream", "--json"],
        )
        self.assertEqual(list_result.exit_code, 0, list_result.output)
        list_payload = _parse_json_output(list_result.output)
        self.assertEqual(list_payload["count"], 0)

    def test_intent_set_triggers_profile_setup_prompt_when_user_profile_missing(self) -> None:
        # This specific test asserts mode="init" — i.e. the chainstream
        # profile is NOT in the user's topic_profiles.yaml. The class setUp
        # seeds it (because the other 3 tests depend on it); override here.
        (self.home / "topic_profiles.yaml").unlink(missing_ok=True)
        runner = CliRunner()
        with (
            patch(
                "agentflow.agent_review.triggers.post_profile_setup_prompt",
                return_value={"gate": "P", "profile_id": "chainstream"},
            ) as post_mock,
            patch("agentflow.shared.agent_bridge.emit_agent_event") as emit_mock,
        ):
            result = runner.invoke(
                cli,
                ["intent-set", "--profile", "chainstream", "--json"],
            )
        self.assertEqual(result.exit_code, 0, result.output)
        post_mock.assert_called_once()
        profile_events = [
            call.kwargs
            for call in emit_mock.call_args_list
            if call.kwargs.get("event_type") == "profile.setup_requested"
        ]
        self.assertEqual(len(profile_events), 1)
        event_kwargs = profile_events[0]
        self.assertEqual(event_kwargs["event_type"], "profile.setup_requested")
        event_payload = event_kwargs["payload"]
        self.assertEqual(event_payload["profile_id"], "chainstream")
        self.assertEqual(event_payload["reason"], "intent-set")
        self.assertEqual(event_payload["mode"], "init")
        self.assertIn("publisher_account.product_facts", event_payload["missing_fields"])
        sessions = list((self.home / "constraint_sessions").glob("*.json"))
        self.assertEqual(len(sessions), 1)
        self.assertEqual(event_payload["session_path"], str(sessions[0]))

    def test_intent_set_allows_new_profile_id_to_bootstrap(self) -> None:
        runner = CliRunner()
        with (
            patch(
                "agentflow.agent_review.triggers.post_profile_setup_prompt",
                return_value={"gate": "P", "profile_id": "uniswap"},
            ),
            patch("agentflow.shared.agent_bridge.emit_agent_event") as emit_mock,
        ):
            result = runner.invoke(
                cli,
                ["intent-set", "--profile", "uniswap", "--json"],
            )
        self.assertEqual(result.exit_code, 0, result.output)
        payload = _parse_json_output(result.output)
        self.assertEqual(payload["intent"]["profile"]["id"], "uniswap")
        self.assertEqual(payload["intent"]["query"]["text"], "uniswap")
        profile_events = [
            call.kwargs
            for call in emit_mock.call_args_list
            if call.kwargs.get("event_type") == "profile.setup_requested"
        ]
        self.assertEqual(len(profile_events), 1)
        self.assertEqual(profile_events[0]["payload"]["profile_id"], "uniswap")

    def test_profile_setup_question_prefers_collected_brand_display(self) -> None:
        text = review_render.render_profile_setup_question(
            profile_id="uniswap-test",
            display_name="Uniswap",
            step_label="Do",
            prompt="请输入 Do 规则。",
            step_index=5,
            total_steps=9,
        )
        self.assertIn("*Profile*  Uniswap", text)
        self.assertIn("*Profile ID*  `uniswap\\-test`", text)

    def test_configure_bot_menu_registers_basic_commands(self) -> None:
        with (
            patch("agentflow.agent_review.daemon.tg_client.set_my_commands") as commands_mock,
            patch("agentflow.agent_review.daemon.tg_client.set_chat_menu_button") as menu_mock,
        ):
            review_daemon.configure_bot_menu(chat_id=456)
        commands_mock.assert_called_once()
        command_names = {
            item["command"] for item in commands_mock.call_args.args[0]
        }
        # v1.0.4 curated set (hyphens become underscores per Telegram's rule):
        # help / status / queue / skip / defer / scan / profile / profiles /
        # profile_switch / style / doctor / audit. Some review-ops basics
        # (status, help, queue) are mandatory; we assert a load-bearing subset
        # rather than the full 12 so the test isn't fragile to ordering.
        self.assertTrue(
            {"help", "status", "queue", "scan", "profile", "doctor"} <= command_names,
            f"missing core v1.0.4 commands; got: {sorted(command_names)}",
        )
        menu_mock.assert_called_once_with(
            chat_id=456,
            menu_button={"type": "commands"},
        )

    def test_tg_cancel_revokes_pending_short_id(self) -> None:
        sid = review_short_id.register(gate="B", article_id="article_cancel_me")
        update = {
            "message": {
                "chat": {"id": 456},
                "from": {"id": 123},
                "text": f"/cancel {sid}",
            }
        }

        with (
            patch("agentflow.agent_review.daemon.auth.is_authorized", return_value=True),
            patch("agentflow.agent_review.daemon.tg_client.send_message") as send_mock,
        ):
            review_daemon._handle_message(update)

        self.assertIsNone(review_short_id.resolve(sid))
        self.assertTrue(review_short_id.was_recently_revoked(sid))
        send_mock.assert_called_once()
        args, kwargs = send_mock.call_args
        self.assertEqual(args[0], 456)
        self.assertIn("已取消", args[1])
        self.assertEqual(kwargs.get("parse_mode"), "MarkdownV2")

    def test_gate_b_edit_callback_registers_pending_edit_from_uid(self) -> None:
        sid = review_short_id.register(gate="B", article_id="article_edit_b")
        entry = review_short_id.resolve(sid)
        with (
            patch("agentflow.agent_review.daemon.auth.is_authorized", return_value=True),
            patch("agentflow.agent_review.daemon.tg_client.answer_callback_query") as answer_mock,
            patch("agentflow.agent_review.daemon.tg_client.send_message"),
        ):
            review_daemon._route(
                "B",
                "edit",
                sid,
                "",
                entry or {},
                "cb1",
                456,
                789,
                123,
            )

        pending = review_daemon.pending_edits.peek(123)
        self.assertIsNotNone(pending)
        self.assertEqual(pending["article_id"], "article_edit_b")
        self.assertEqual(pending["gate"], "B")
        answer_mock.assert_called_once()

    def test_gate_l_edit_callback_registers_pending_edit_from_chat_fallback(self) -> None:
        sid = review_short_id.register(gate="L", article_id="article_edit_l")
        entry = review_short_id.resolve(sid)
        with (
            patch("agentflow.agent_review.daemon.auth.is_authorized", return_value=True),
            patch("agentflow.agent_review.daemon.tg_client.answer_callback_query") as answer_mock,
            patch("agentflow.agent_review.daemon.tg_client.send_message"),
        ):
            review_daemon._route(
                "L",
                "edit",
                sid,
                "",
                entry or {},
                "cb1",
                456,
                789,
                None,
            )

        pending = review_daemon.pending_edits.peek(456)
        self.assertIsNotNone(pending)
        self.assertEqual(pending["article_id"], "article_edit_l")
        self.assertEqual(pending["gate"], "L")
        answer_mock.assert_called_once()

    def test_hotspots_creates_learning_suggestion_and_passes_panel_context(self) -> None:
        runner = CliRunner()
        runner.invoke(cli, ["intent-set", "--profile", "chainstream"])
        output = D1Output(
            generated_at=datetime.now(timezone.utc),
            hotspots=[
                Hotspot(
                    id="hs_chainstream",
                    topic_one_liner="Kafka Streams for multi-chain on-chain data",
                    source_references=[{"text_snippet": "MCP plus token-prices stream"}],
                    mainstream_views=[],
                    overlooked_angles=[],
                    recommended_series="C",
                    series_confidence=0.9,
                    suggested_angles=[
                        SuggestedAngle(
                            angle="Infra comparison",
                            fit_explanation="good",
                            depth="medium",
                            difficulty="medium",
                        )
                    ],
                    freshness_score=0.8,
                    depth_potential="high",
                    generated_at=datetime.now(timezone.utc),
                ),
                Hotspot(
                    id="hs_other",
                    topic_one_liner="Agent observability for database incidents",
                    source_references=[{"text_snippet": "incident tracing"}],
                    mainstream_views=[],
                    overlooked_angles=[],
                    recommended_series="A",
                    series_confidence=0.6,
                    suggested_angles=[
                        SuggestedAngle(
                            angle="Why tracing matters",
                            fit_explanation="maybe",
                            depth="medium",
                            difficulty="medium",
                        )
                    ],
                    freshness_score=0.7,
                    depth_potential="medium",
                    generated_at=datetime.now(timezone.utc),
                ),
            ],
        )
        with (
            patch.dict(os.environ, {"MOCK_LLM": "true"}, clear=False),
            patch("agentflow.agent_d1.main.run_d1_scan", return_value=output),
            patch(
                "agentflow.agent_d1.search.run_d1_search",
                return_value=(
                    D1Output(generated_at=datetime.now(timezone.utc), hotspots=[]),
                    self.home / "search_results" / "empty.json",
                ),
            ),
            patch(
                "agentflow.agent_review.triggers.post_gate_a",
                return_value={"gate": "A"},
            ) as gate_mock,
            patch(
                "agentflow.agent_review.triggers.post_profile_setup_prompt",
                return_value=None,
            ),
        ):
            result = runner.invoke(cli, ["hotspots", "--json"])
        self.assertEqual(result.exit_code, 0, result.output)
        gate_mock.assert_called_once()
        kwargs = gate_mock.call_args.kwargs
        self.assertTrue(kwargs["config_suggestions"])
        self.assertIn("boundary", kwargs["filter_meta"])
        suggestions = list((self.home / "constraint_suggestions").glob("*.json"))
        self.assertTrue(suggestions)

    def test_profile_session_reply_advances_and_applies(self) -> None:
        session_id = "session_profile_flow"
        save_session(
            {
                "id": session_id,
                "profile_id": "chainstream",
                "mode": "init",
                "status": "collecting",
                "active_uid": 123,
                "active_chat_id": 456,
                "step_index": 0,
                "answers": {},
            }
        )
        with (
            patch("agentflow.agent_review.daemon.tg_client.send_message"),
            patch("agentflow.agent_review.daemon._spawn_apply_profile_session") as spawn_mock,
        ):
            self.assertTrue(
                review_daemon._maybe_handle_profile_session_reply(
                    chat_id=456,
                    uid=123,
                    text="ChainStream",
                )
            )
            self.assertTrue(
                review_daemon._maybe_handle_profile_session_reply(
                    chat_id=456,
                    uid=123,
                    text="first_party_brand, 简体中文, 用我们开口",
                )
            )
            self.assertTrue(
                review_daemon._maybe_handle_profile_session_reply(
                    chat_id=456,
                    uid=123,
                    text="AI-native crypto infra\non-chain data\nKafka Streams\nsmart money",
                )
            )
            self.assertTrue(
                review_daemon._maybe_handle_profile_session_reply(
                    chat_id=456,
                    uid=123,
                    text="Do: 先讲工程事实\nDon't: 不要第三方口吻\nAvoid: general AI hype",
                )
            )
        session = json.loads((self.home / "constraint_sessions" / f"{session_id}.json").read_text(encoding="utf-8"))
        self.assertEqual(session["status"], "completed")
        self.assertIn("profile_patch", session)
        self.assertEqual(
            session["profile_patch"]["publisher_account"]["output_language"],
            "zh-Hans",
        )
        self.assertIn("Kafka Streams", session["profile_patch"]["search_queries"])
        self.assertIn("general AI hype", session["profile_patch"]["avoid_terms"])
        spawn_mock.assert_called_once()

    def test_tg_suggestions_lists_pending_items(self) -> None:
        suggestion_file = self.home / "patch.json"
        suggestion_file.write_text(
            json.dumps(
                {
                    "proposed_patch": {
                        "search_queries": ["smart money"],
                    },
                    "evidence": [{"stage": "search"}],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "topic-profile",
                "suggest",
                "--profile",
                "chainstream",
                "--stage",
                "search",
                "--title",
                "Search widening",
                "--summary",
                "Promote query",
                "--from-file",
                str(suggestion_file),
                "--json",
            ],
        )
        self.assertEqual(result.exit_code, 0, result.output)

        update = {
            "message": {
                "chat": {"id": 456},
                "from": {"id": 123},
                "text": "/suggestions chainstream",
            }
        }
        with (
            patch("agentflow.agent_review.daemon.auth.is_authorized", return_value=True),
            patch("agentflow.agent_review.daemon.tg_client.send_message") as send_mock,
        ):
            review_daemon._handle_message(update)
        send_mock.assert_called_once()
        args, kwargs = send_mock.call_args
        self.assertEqual(args[0], 456)
        self.assertIn("Pending Profile Suggestions", args[1])
        self.assertTrue(kwargs.get("reply_markup"))

    def test_tg_list_includes_ready_filters_and_truncates(self) -> None:
        self.stack.enter_context(
            patch.object(review_daemon, "_REVIEW_HOME", self.home / "review")
        )

        def seed_article(article_id: str, title: str, to_state: str, gate: str) -> None:
            draft_dir = self.home / "drafts" / article_id
            draft_dir.mkdir(parents=True, exist_ok=True)
            (draft_dir / "metadata.json").write_text(
                json.dumps(
                    {
                        "article_id": article_id,
                        "title": title,
                        "gate_history": [
                            {
                                "gate": gate,
                                "from_state": "seed",
                                "to_state": to_state,
                                "actor": "test",
                                "decision": "seed",
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                                "round": 0,
                            }
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

        seed_article(
            "draft001long",
            "Draft pending",
            review_state.STATE_DRAFT_PENDING_REVIEW,
            "B",
        )
        seed_article(
            "image001long",
            "Image pending",
            review_state.STATE_IMAGE_PENDING_REVIEW,
            "C",
        )
        seed_article(
            "chan001long",
            "Channel pending",
            review_state.STATE_CHANNEL_PENDING_REVIEW,
            "D",
        )
        for idx in range(21):
            seed_article(
                f"ready{idx:03d}long",
                f"Ready title {idx}",
                review_state.STATE_READY_TO_PUBLISH,
                "P",
            )

        def handle_list(text: str) -> str:
            update = {
                "message": {
                    "chat": {"id": 456},
                    "from": {"id": 123},
                    "text": text,
                }
            }
            with (
                patch("agentflow.agent_review.daemon.auth.is_authorized", return_value=True),
                patch("agentflow.agent_review.daemon.tg_client.send_message") as send_mock,
            ):
                review_daemon._handle_message(update)
            send_mock.assert_called_once()
            args, _kwargs = send_mock.call_args
            self.assertEqual(args[0], 456)
            return args[1]

        all_body = handle_list("/list")
        self.assertIn("Ready", all_body)
        self.assertIn("Ready title 0", all_body)
        self.assertIn("还有 4 条", all_body)
        self.assertIn("/list <gate\\>", all_body)

        image_body = handle_list("/list c")
        self.assertIn("C", image_body)
        self.assertIn("Image pending", image_body)
        self.assertNotIn("Draft pending", image_body)
        self.assertNotIn("Ready title", image_body)

        ready_body = handle_list("/list publish")
        self.assertIn("Ready", ready_body)
        self.assertIn("Ready title 0", ready_body)
        self.assertNotIn("Image pending", ready_body)

        help_body = handle_list("/list unknown")
        self.assertIn("用法", help_body)
        self.assertIn("ready", help_body)

        audit_events = [
            json.loads(line)
            for line in (self.home / "review" / "audit.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
        ]
        list_events = [event for event in audit_events if event.get("cmd") == "/list"]
        self.assertEqual(list_events[-4]["filter"], "all")
        self.assertEqual(list_events[-4]["total"], 24)
        self.assertEqual(list_events[-2]["filter"], "publish")
        self.assertEqual(list_events[-2]["total"], 21)

    def test_tg_suggestion_apply_callback_updates_profile(self) -> None:
        runner = CliRunner()
        base_session_id = "session_suggestion_apply_base"
        save_session(
            {
                "id": base_session_id,
                "profile_id": "chainstream",
                "mode": "init",
                "status": "completed",
                "answers": {
                    "brand": "ChainStream",
                    "voice": "first_party_brand",
                    "product_facts": ["AI-native crypto infra"],
                    "do": ["用我们开口"],
                    "dont": ["不要第三方口吻"],
                    "core_terms": ["on-chain data"],
                    "search_queries": ["on-chain data"],
                    "avoid_terms": ["general AI hype"],
                },
            }
        )
        runner.invoke(
            cli,
            [
                "topic-profile",
                "init",
                "--profile",
                "chainstream",
                "--from-session",
                base_session_id,
            ],
        )
        suggestion_file = self.home / "apply_patch.json"
        suggestion_file.write_text(
            json.dumps(
                {
                    "proposed_patch": {
                        "search_queries": ["on-chain data", "agent observability"],
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        suggest_result = runner.invoke(
            cli,
            [
                "topic-profile",
                "suggest",
                "--profile",
                "chainstream",
                "--stage",
                "search",
                "--title",
                "Search widening",
                "--summary",
                "Promote query",
                "--from-file",
                str(suggestion_file),
                "--json",
            ],
        )
        self.assertEqual(suggest_result.exit_code, 0, suggest_result.output)
        suggestion_payload = _parse_json_output(suggest_result.output)
        sid = review_short_id.register(
            gate="S",
            batch_path=suggestion_payload["path"],
            extra={"suggestion_id": suggestion_payload["id"]},
        )
        entry = review_short_id.resolve(sid)
        with (
            patch("agentflow.agent_review.daemon.auth.is_authorized", return_value=True),
            patch("agentflow.agent_review.daemon.tg_client.answer_callback_query") as answer_mock,
            patch("agentflow.agent_review.daemon.tg_client.send_message"),
            patch("agentflow.agent_review.daemon.tg_client.edit_message_reply_markup"),
        ):
            review_daemon._route(
                "S",
                "apply",
                sid,
                "",
                entry or {},
                "cb1",
                456,
                789,
                123,
            )
        answer_mock.assert_called()
        applied = json.loads(
            (
                self.home
                / "constraint_suggestions"
                / f"{suggestion_payload['id']}.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(applied["status"], "applied")
        show_result = runner.invoke(
            cli,
            ["topic-profile", "show", "--profile", "chainstream", "--json"],
        )
        show_payload = _parse_json_output(show_result.output)
        self.assertIn(
            "agent observability",
            show_payload["current_profile"]["search_queries"],
        )


class NewsletterCorrectionTests(AgentflowHomeTestCase):
    def test_newsletter_correction_updates_metadata_history_and_memory(self) -> None:
        email_storage.save_newsletter(
            "nl_test_correction",
            subject="Weekly note",
            preview_text="Preview",
            html_body="<p>Hello</p>",
            plain_text_body="Hello",
            article_id="art1",
            status="sent",
            extra={"last_platform_post_id": "resend_prev_123"},
        )

        runner = CliRunner()
        with patch.dict(
            os.environ,
            {
                "MOCK_LLM": "true",
                "NEWSLETTER_AUDIENCE_ID": "aud_test",
                "NEWSLETTER_FROM_EMAIL": "bot@example.com",
            },
            clear=False,
        ):
            result = runner.invoke(
                cli, ["newsletter-correction", "nl_test_correction", "--json"]
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = _parse_json_output(result.output)
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["audience_id"], "aud_test")
        self.assertEqual(
            payload["correction_of_platform_post_id"], "resend_prev_123"
        )
        self.assertTrue(payload["correction_subject"].startswith("Correction: "))

        stored = email_storage.load_newsletter("nl_test_correction")
        self.assertEqual(stored["status"], "sent")
        self.assertEqual(stored["correction_count"], 1)
        self.assertEqual(
            stored["last_correction_of_platform_post_id"], "resend_prev_123"
        )

        history = d4_storage.read_publish_history("art1")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["platform"], "email_newsletter")
        self.assertEqual(history[0]["status"], "success")

        events = memory.read_memory_events(article_id="art1")
        self.assertEqual(events[-1]["event_type"], "newsletter_correction_sent")


class ImageAutoResolveTests(AgentflowHomeTestCase):
    def test_image_auto_resolve_persists_resolved_paths(self) -> None:
        article_id = "article_auto_resolve"
        draft = DraftOutput(
            article_id=article_id,
            title="Test Draft",
            sections=[
                FilledSection(
                    heading="Section A",
                    content_markdown="[IMAGE: subagent 并行执行示意图]",
                    word_count=10,
                    compliance_score=1.0,
                ),
                FilledSection(
                    heading="Section B",
                    content_markdown="[IMAGE: subagent 并行执行示意图]",
                    word_count=10,
                    compliance_score=1.0,
                ),
            ],
            total_word_count=20,
            image_placeholders=[
                ImagePlaceholder(
                    id=f"{article_id}_1",
                    description="subagent 并行执行示意图",
                    section_heading="Section A",
                ),
                ImagePlaceholder(
                    id=f"{article_id}_2",
                    description="subagent 并行执行示意图",
                    section_heading="Section B",
                ),
            ],
        )
        save_draft(draft)

        library = self.home / "fixtures" / "images"
        library.mkdir(parents=True, exist_ok=True)
        image_path = library / "subagent_并行执行示意图.png"
        image_path.write_text("not-a-real-png", encoding="utf-8")

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "image-auto-resolve",
                article_id,
                "--library",
                str(library),
                "--json",
            ],
        )
        self.assertEqual(result.exit_code, 0, result.output)
        payload = _parse_json_output(result.output)
        self.assertEqual(payload["auto_resolved_count"], 2)
        self.assertEqual(payload["remaining_unresolved_count"], 0)

        reloaded = DraftOutput.from_dict(
            json.loads(
                (self.home / "drafts" / article_id / "metadata.json").read_text(
                    encoding="utf-8"
                )
            )
        )
        resolved = {p.resolved_path for p in reloaded.image_placeholders}
        self.assertEqual(resolved, {str(image_path.resolve())})

        events = memory.read_memory_events(article_id=article_id)
        self.assertEqual(events[-1]["event_type"], "images_auto_resolved")


class ImageGenerateTests(AgentflowHomeTestCase):
    def test_image_generate_creates_file_and_resolves_placeholder(self) -> None:
        article_id = "article_generate_image"
        draft = DraftOutput(
            article_id=article_id,
            title="Kafka Streams article",
            sections=[
                FilledSection(
                    heading="系统架构",
                    content_markdown="[IMAGE: 多链实时数据流向 Kafka Streams 的架构图]",
                    word_count=12,
                    compliance_score=1.0,
                )
            ],
            total_word_count=12,
            image_placeholders=[
                ImagePlaceholder(
                    id=f"{article_id}_1",
                    description="多链实时数据流向 Kafka Streams 的架构图",
                    section_heading="系统架构",
                )
            ],
        )
        save_draft(draft)

        class _Resp:
            def __init__(
                self,
                *,
                status_code: int = 200,
                json_data: dict | None = None,
                content: bytes = b"",
                headers: dict[str, str] | None = None,
            ) -> None:
                self.status_code = status_code
                self._json_data = json_data or {}
                self.content = content
                self.headers = headers or {}

            def raise_for_status(self) -> None:
                if self.status_code >= 400:
                    raise RuntimeError(f"http {self.status_code}")

            def json(self) -> dict:
                return self._json_data

        def _fake_post(url: str, headers: dict | None = None, json: dict | None = None, timeout: int = 30):
            self.assertIn("/generateImage", url)
            self.assertEqual(json["model"], "openai/gpt-image-2-developer/text-to-image")
            return _Resp(
                json_data={
                    "code": 200,
                    "message": "ok",
                    "data": {
                        "id": "req_123",
                        "status": "created",
                        "model": json["model"],
                    },
                }
            )

        def _fake_get(url: str, headers: dict | None = None, timeout: int = 30):
            if "/prediction/" in url:
                return _Resp(
                    json_data={
                        "code": 200,
                        "message": "ok",
                        "data": {
                            "id": "req_123",
                            "status": "completed",
                            "outputs": ["https://cdn.example.com/generated/cover.png"],
                        },
                    }
                )
            self.assertEqual(url, "https://cdn.example.com/generated/cover.png")
            return _Resp(
                content=b"fake-png-binary",
                headers={"Content-Type": "image/png"},
            )

        runner = CliRunner()
        with (
            patch.dict(
                os.environ,
                {
                    "ATLASCLOUD_API_KEY": "test_key",
                    "AGENTFLOW_GENERATED_IMAGE_DIR": str(self.home / "generated-images"),
                },
                clear=False,
            ),
            patch("agentflow.agent_d2.image_generator.requests.post", side_effect=_fake_post),
            patch("agentflow.agent_d2.image_generator.requests.get", side_effect=_fake_get),
            patch("agentflow.agent_d2.image_generator.time.sleep", return_value=None),
        ):
            result = runner.invoke(cli, ["image-generate", article_id, "--json"])

        self.assertEqual(result.exit_code, 0, result.output)
        payload = _parse_json_output(result.output)
        self.assertEqual(payload["generated_count"], 1)
        saved_path = Path(payload["generated"][0]["saved_path"])
        self.assertTrue(saved_path.exists())

        reloaded = DraftOutput.from_dict(
            json.loads(
                (self.home / "drafts" / article_id / "metadata.json").read_text(
                    encoding="utf-8"
                )
            )
        )
        self.assertEqual(reloaded.image_placeholders[0].resolved_path, str(saved_path))

        events = memory.read_memory_events(article_id=article_id)
        self.assertEqual(events[-1]["event_type"], "images_generated")


class ImagePromptHintTests(AgentflowHomeTestCase):
    def test_domain_hint_prefers_profile_publisher_account_hints(self) -> None:
        with (
            patch.dict(os.environ, {"AGENTFLOW_IMAGE_DOMAIN_HINT": ""}, clear=False),
            patch(
                "agentflow.shared.memory.load_current_intent",
                return_value={"profile": {"id": "chainstream"}},
            ),
            patch(
                "agentflow.shared.topic_profiles.resolve_publisher_account_from_intent",
                return_value={
                    "image_prompt_hints": [
                        "real-time data pipelines",
                        "infra diagrams",
                    ]
                },
            ),
        ):
            hint = image_generator._domain_hint()

        self.assertEqual(hint, "real-time data pipelines, infra diagrams")


class LLMFallbackTests(unittest.TestCase):
    def test_dispatch_chat_falls_back_from_kimi_to_claude_when_enabled(self) -> None:
        client = LLMClient()

        class _RateLimitError(RuntimeError):
            status_code = 429

        with (
            patch.dict(
                os.environ,
                {
                    "GENERATION_PROVIDER": "kimi",
                    "GENERATION_PROVIDER_FALLBACK": "true",
                    "ANTHROPIC_API_KEY": "test-key",
                },
                clear=False,
            ),
            patch.object(client, "_kimi_call", side_effect=_RateLimitError("429")) as kimi_mock,
            patch.object(client, "_claude_call", return_value="fallback ok") as claude_mock,
        ):
            result = asyncio.run(
                client._dispatch_chat(
                    prompt="hello",
                    system=None,
                    max_tokens=128,
                    prompt_family="unit_test",
                )
            )

        self.assertEqual(result, "fallback ok")
        kimi_mock.assert_called_once()
        claude_mock.assert_called_once()
        self.assertEqual(claude_mock.call_args.kwargs["log_extra"]["fallback_from"], "kimi")

    def test_dispatch_chat_does_not_fallback_when_disabled(self) -> None:
        client = LLMClient()

        class _RateLimitError(RuntimeError):
            status_code = 429

        with (
            patch.dict(
                os.environ,
                {
                    "GENERATION_PROVIDER": "kimi",
                    "GENERATION_PROVIDER_FALLBACK": "false",
                    "ANTHROPIC_API_KEY": "test-key",
                },
                clear=False,
            ),
            patch.object(client, "_kimi_call", side_effect=_RateLimitError("429")) as kimi_mock,
            patch.object(client, "_claude_call", return_value="fallback ok") as claude_mock,
        ):
            with self.assertRaises(_RateLimitError):
                asyncio.run(
                    client._dispatch_chat(
                        prompt="hello",
                        system=None,
                        max_tokens=128,
                        prompt_family="unit_test",
                    )
                )

        kimi_mock.assert_called_once()
        claude_mock.assert_not_called()


class LocalMockPipelineTests(AgentflowHomeTestCase):
    def test_image_gate_none_transitions_to_image_skipped_and_posts_gate_d(self) -> None:
        article_id = "article_image_none"
        draft = DraftOutput(
            article_id=article_id,
            title="Skip images",
            sections=[
                FilledSection(
                    heading="Intro",
                    content_markdown="[IMAGE: optional chart]\n\nBody.",
                    word_count=4,
                    compliance_score=1.0,
                )
            ],
            total_word_count=4,
            image_placeholders=[
                ImagePlaceholder(
                    id=f"{article_id}_1",
                    description="optional chart",
                    section_heading="Intro",
                )
            ],
        )
        save_draft(draft)
        review_state.transition(
            article_id,
            gate="B",
            to_state=review_state.STATE_DRAFT_APPROVED,
            actor="test",
            decision="seed_draft_approved",
            force=True,
        )

        runner = CliRunner()
        with patch(
            "agentflow.agent_review.triggers.post_gate_d",
            return_value={"short_id": "retryd1"},
        ) as post_gate_d_mock:
            result = runner.invoke(
                cli,
                ["image-gate", article_id, "--mode", "none", "--json"],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = _parse_json_output(result.output)
        self.assertEqual(payload["mode"], "none")
        self.assertIn("gate_d_short_id", payload)
        self.assertEqual(
            review_state.current_state(article_id),
            review_state.STATE_IMAGE_SKIPPED,
        )
        gate_history = review_state.gate_history(article_id)
        self.assertEqual(gate_history[-1]["decision"], "image_mode_none")
        post_gate_d_mock.assert_called_once_with(article_id)

    def test_local_mock_pipeline_closes_loop(self) -> None:
        runner = CliRunner()

        with patch.dict(
            os.environ,
            {
                "MOCK_LLM": "true",
                "AGENTFLOW_MOCK_PUBLISHERS": "true",
                "AGENTFLOW_ALLOW_MOCK_URLS": "true",
                "TELEGRAM_BOT_TOKEN": "",
                "TELEGRAM_REVIEW_CHAT_ID": "",
            },
            clear=False,
        ):
            hotspots_result = runner.invoke(cli, ["hotspots", "--json"])
            self.assertEqual(hotspots_result.exit_code, 0, hotspots_result.output)
            hotspots_payload = _parse_json_output(hotspots_result.output)
            self.assertTrue(hotspots_payload["hotspots"], hotspots_result.output)
            hotspot_id = hotspots_payload["hotspots"][0]["id"]

            write_result = runner.invoke(
                cli,
                ["write", hotspot_id, "--auto-pick", "--json"],
            )
            self.assertEqual(write_result.exit_code, 0, write_result.output)
            write_payload = _parse_json_output(write_result.output)
            article_id = write_payload["article_id"]

            image_gate_result = runner.invoke(
                cli,
                ["image-gate", article_id, "--mode", "none", "--json"],
            )
            self.assertEqual(image_gate_result.exit_code, 0, image_gate_result.output)

            preview_result = runner.invoke(
                cli,
                [
                    "preview",
                    article_id,
                    "--platforms",
                    "medium,ghost_wordpress",
                    "--skip-images",
                    "--json",
                ],
            )
            self.assertEqual(preview_result.exit_code, 0, preview_result.output)
            preview_payload = _parse_json_output(preview_result.output)
            preview_platforms = [v["platform"] for v in preview_payload["platform_versions"]]
            self.assertEqual(preview_platforms, ["medium", "ghost_wordpress"])

            publish_result = runner.invoke(
                cli,
                [
                    "publish",
                    article_id,
                    "--platforms",
                    "ghost_wordpress",
                    "--force-strip-images",
                    "--json",
                ],
            )
            self.assertEqual(publish_result.exit_code, 0, publish_result.output)
            publish_payload = _parse_json_output(publish_result.output)
            self.assertEqual(len(publish_payload["results"]), 1)
            ghost_result = publish_payload["results"][0]
            self.assertEqual(ghost_result["platform"], "ghost_wordpress")
            self.assertEqual(ghost_result["status"], "success")
            self.assertTrue(str(ghost_result["published_url"]).startswith("https://"))

            package_result = runner.invoke(
                cli,
                [
                    "medium-package",
                    article_id,
                    "--distribution-mode",
                    "cross_post",
                    "--json",
                ],
            )
            self.assertEqual(package_result.exit_code, 0, package_result.output)
            package_payload = _parse_json_output(package_result.output)
            self.assertEqual(package_payload["canonical_url"], ghost_result["published_url"])

            mark_result = runner.invoke(
                cli,
                [
                    "review-publish-mark",
                    article_id,
                    "https://medium.com/@mock/local-pipeline-smoke",
                    "--platform",
                    "medium",
                    "--json",
                ],
            )
            self.assertEqual(mark_result.exit_code, 0, mark_result.output)
            mark_payload = _parse_json_output(mark_result.output)
            self.assertEqual(mark_payload["platform"], "medium")
            self.assertEqual(mark_payload["published_url"], "https://medium.com/@mock/local-pipeline-smoke")

            status_result = runner.invoke(cli, ["review-status", article_id, "--json"])
            self.assertEqual(status_result.exit_code, 0, status_result.output)
            status_payload = _parse_json_output(status_result.output)
            self.assertEqual(status_payload["current_state"], "published")

            history = d4_storage.read_publish_history(article_id)
            self.assertEqual(len(history), 2)
            self.assertEqual({rec["platform"] for rec in history}, {"ghost_wordpress", "medium"})
            self.assertTrue(all(rec["status"] == "success" for rec in history))


class AgentBridgeEventTests(AgentflowHomeTestCase):
    def test_agent_bridge_fans_out_memory_gate_and_publish_events(self) -> None:
        article_id = "bridge_article"
        draft_dir = self.home / "drafts" / article_id
        draft_dir.mkdir(parents=True, exist_ok=True)
        (draft_dir / "metadata.json").write_text(
            json.dumps({"article_id": article_id}, ensure_ascii=False),
            encoding="utf-8",
        )

        with (
            patch.dict(
                os.environ,
                {
                    "AGENTFLOW_AGENT_EVENT_WEBHOOK_URL": "https://agent.example.com/events",
                    "AGENTFLOW_AGENT_EVENT_AUTH_HEADER": "Bearer bridge-secret",
                },
                clear=False,
            ),
            patch("agentflow.shared.agent_bridge.requests.post") as post_mock,
        ):
            memory.append_memory_event(
                "article_created",
                article_id=article_id,
                payload={"stage": "draft"},
            )
            review_state.transition(
                article_id,
                gate="A",
                to_state=review_state.STATE_TOPIC_APPROVED,
                actor="system",
                decision="approve",
            )
            d4_storage.append_publish_record(
                article_id,
                PublishResult(
                    platform="ghost_wordpress",
                    status="success",
                    published_url="https://ghost.example.com/post",
                    platform_post_id="ghost_1",
                    published_at=datetime.now(timezone.utc),
                ),
            )

        self.assertEqual(post_mock.call_count, 3)
        emitted = [json.loads(call.kwargs["data"]) for call in post_mock.call_args_list]
        self.assertEqual([item["source"] for item in emitted], ["memory", "gate", "publish"])
        self.assertEqual(emitted[0]["event_type"], "article_created")
        self.assertEqual(emitted[1]["event_type"], "gate.transition")
        self.assertEqual(emitted[2]["event_type"], "publish.record")
        self.assertTrue(all(item["event_id"].startswith("evt_") for item in emitted))


class PublishStatsTests(AgentflowHomeTestCase):
    def test_fetch_stats_supports_ghost_wordpress_alias(self) -> None:
        from agentflow.agent_review import stats_fetchers

        class _Resp:
            status_code = 200

            def json(self) -> dict:
                return {
                    "posts": [
                        {
                            "id": "ghost_123",
                            "title": "Ghost post",
                            "status": "published",
                            "visibility": "public",
                            "published_at": "2026-04-28T00:00:00.000Z",
                            "updated_at": "2026-04-28T01:00:00.000Z",
                            "count": {"clicks": 7, "mentions": 2},
                        }
                    ]
                }

        with (
            patch.dict(
                os.environ,
                {
                    "GHOST_ADMIN_API_URL": "https://ghost.example.com",
                    "GHOST_ADMIN_API_KEY": f"{'a' * 24}:{'b' * 64}",
                },
                clear=False,
            ),
            patch("requests.get", return_value=_Resp()) as get_mock,
        ):
            result = stats_fetchers.fetch_stats(
                "ghost_wordpress",
                "https://ghost.example.com/post",
                "ghost_123",
            )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["scrape_status"], "ok")
        self.assertEqual(result["clicks"], 7)
        self.assertEqual(result["mentions"], 2)
        self.assertIn("/ghost/api/admin/posts/ghost_123/", get_mock.call_args.args[0])

    def test_fetch_stats_supports_linkedin_article_alias(self) -> None:
        from agentflow.agent_review import stats_fetchers

        class _Resp:
            status_code = 200

            def json(self) -> dict:
                return {
                    "likesSummary": {"totalLikes": 11},
                    "commentsSummary": {"aggregatedTotalComments": 3},
                }

        with (
            patch.dict(os.environ, {"LINKEDIN_ACCESS_TOKEN": "li-token"}, clear=False),
            patch("requests.get", return_value=_Resp()) as get_mock,
        ):
            result = stats_fetchers.fetch_stats(
                "linkedin_article",
                "https://www.linkedin.com/feed/update/urn:li:share:123",
                "urn:li:share:123",
            )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["scrape_status"], "ok")
        self.assertEqual(result["likes"], 11)
        self.assertEqual(result["comments"], 3)
        self.assertIn("socialActions/urn%3Ali%3Ashare%3A123", get_mock.call_args.args[0])

    def test_fetch_stats_supports_webhook_stats_endpoint(self) -> None:
        from agentflow.agent_review import stats_fetchers

        class _Resp:
            status_code = 200

            def json(self) -> dict:
                return {"likes": 5, "comments": 1}

        with (
            patch.dict(
                os.environ,
                {
                    "AGENTFLOW_WEBHOOK_STATS_URL": "https://cms.example.com/stats",
                    "AGENTFLOW_WEBHOOK_STATS_AUTH_HEADER": "Bearer stats-token",
                },
                clear=False,
            ),
            patch("requests.get", return_value=_Resp()) as get_mock,
        ):
            result = stats_fetchers.fetch_stats(
                "webhook",
                "https://cms.example.com/posts/a",
                "cms_1",
            )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["scrape_status"], "ok")
        self.assertEqual(result["likes"], 5)
        self.assertEqual(result["comments"], 1)
        self.assertEqual(get_mock.call_args.args[0], "https://cms.example.com/stats")
        self.assertEqual(
            get_mock.call_args.kwargs["headers"]["Authorization"],
            "Bearer stats-token",
        )
        self.assertEqual(
            get_mock.call_args.kwargs["params"]["platform_url"],
            "https://cms.example.com/posts/a",
        )

    def test_review_publish_stats_writes_platform_snapshot(self) -> None:
        article_id = "article_stats"
        draft = DraftOutput(
            article_id=article_id,
            title="Stats test",
            sections=[
                FilledSection(
                    heading="Intro",
                    content_markdown="Body.",
                    word_count=1,
                    compliance_score=1.0,
                )
            ],
            total_word_count=1,
            image_placeholders=[],
        )
        save_draft(draft)
        review_state.transition(
            article_id,
            gate="D",
            to_state=review_state.STATE_PUBLISHED,
            actor="test",
            decision="seed_published",
            force=True,
        )
        d4_storage.append_publish_record(
            article_id,
            PublishResult(
                platform="ghost_wordpress",
                status="success",
                published_url="https://ghost.example.com/post",
                platform_post_id="ghost_123",
            ),
        )

        runner = CliRunner()
        with patch(
            "agentflow.agent_review.stats_fetchers.fetch_stats",
            return_value={
                "fetched_at": "2026-04-28T00:00:00+00:00",
                "platform_url": "https://ghost.example.com/post",
                "clicks": 9,
                "scrape_status": "ok",
            },
        ) as fetch_mock:
            result = runner.invoke(cli, ["review-publish-stats", article_id, "--json"])

        self.assertEqual(result.exit_code, 0, result.output)
        payload = _parse_json_output(result.output)
        self.assertEqual(payload["platforms"]["ghost_wordpress"]["clicks"], 9)
        fetch_mock.assert_called_once_with(
            "ghost_wordpress",
            "https://ghost.example.com/post",
            "ghost_123",
        )
        meta = json.loads(
            (self.home / "drafts" / article_id / "metadata.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(meta["publish_stats"]["ghost_wordpress"]["clicks"], 9)


class AgentBridgeApiTests(AgentflowHomeTestCase):
    def test_bridge_descriptor_and_command_endpoint_work(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "REVIEW_DASHBOARD_TOKEN": "read-token",
                    "AGENTFLOW_AGENT_BRIDGE_TOKEN": "write-token",
                },
                clear=False,
            ),
            patch("agentflow.agent_review.web.emit_agent_event") as emit_mock,
            patch(
                "agentflow.agent_review.web.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=["af", "doctor", "--json"],
                    returncode=0,
                    stdout='{"status":"ok"}',
                    stderr="",
                ),
            ) as run_mock,
        ):
            client = TestClient(create_app())

            bridge_res = client.get(
                "/api/bridge",
                headers={"Authorization": "Bearer read-token"},
            )
            self.assertEqual(bridge_res.status_code, 200)
            bridge_payload = bridge_res.json()
            self.assertIn("doctor", bridge_payload["commands"])
            self.assertEqual(bridge_payload["bridge_version"], "1.0")
            self.assertIn("stability", bridge_payload)

            schema_res = client.get(
                "/api/bridge/schema",
                headers={"Authorization": "Bearer read-token"},
            )
            self.assertEqual(schema_res.status_code, 200)
            schema_payload = schema_res.json()
            self.assertEqual(schema_payload["bridge_version"], "1.0")
            self.assertIn("command_request_schema", schema_payload)

            cmd_res = client.post(
                "/api/commands",
                headers={"Authorization": "Bearer write-token"},
                json={"command": "doctor"},
            )
            self.assertEqual(cmd_res.status_code, 200)
            cmd_payload = cmd_res.json()
            self.assertTrue(cmd_payload["ok"])
            self.assertEqual(cmd_payload["command"], "doctor")
            run_mock.assert_called_once()
            self.assertGreaterEqual(emit_mock.call_count, 2)

    def test_dangerous_command_stays_blocked_by_default(self) -> None:
        with patch.dict(
            os.environ,
            {
                "AGENTFLOW_AGENT_BRIDGE_TOKEN": "write-token",
                "AGENTFLOW_AGENT_BRIDGE_ENABLE_DANGEROUS": "false",
            },
            clear=False,
        ):
            client = TestClient(create_app())
            res = client.post(
                "/api/commands",
                headers={"Authorization": "Bearer write-token"},
                json={"command": "publish", "params": {"article_id": "art1"}},
            )
            self.assertEqual(res.status_code, 403)


class PreferencesIntentTests(unittest.TestCase):
    def test_rebuild_from_events_emits_intent_section(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            events_path = Path(tmpdir) / "events.jsonl"
            records = [
                {
                    "schema_version": 1,
                    "ts": "2026-04-24T10:00:00+00:00",
                    "event_type": "intent_used_in_write",
                    "article_id": "a1",
                    "hotspot_id": None,
                    "payload": {
                        "query": "MCP server",
                        "stage": "skeleton",
                        "ttl": "session",
                        "profile_id": "chainstream",
                        "profile_label": "ChainStream",
                    },
                },
                {
                    "schema_version": 1,
                    "ts": "2026-04-24T11:00:00+00:00",
                    "event_type": "intent_used_in_write",
                    "article_id": "a1",
                    "hotspot_id": None,
                    "payload": {
                        "query": "MCP server",
                        "stage": "fill",
                        "ttl": "session",
                        "profile_id": "chainstream",
                        "profile_label": "ChainStream",
                    },
                },
                {
                    "schema_version": 1,
                    "ts": "2026-04-24T12:00:00+00:00",
                    "event_type": "topic_intent_set",
                    "article_id": None,
                    "hotspot_id": None,
                    "payload": {
                        "query": "multi-agent orchestration",
                        "mode": "keyword",
                        "ttl": "persistent",
                        "source": "cli_flag",
                        "profile_id": "chainstream",
                        "profile_label": "ChainStream",
                    },
                },
            ]
            events_path.write_text(
                "\n".join(json.dumps(record, ensure_ascii=False) for record in records)
                + "\n",
                encoding="utf-8",
            )

            rebuilt = preferences.rebuild_from_events(path=events_path)

        self.assertIn("intent", rebuilt)
        self.assertEqual(rebuilt["intent"]["recent_queries"][0]["query"], "MCP server")
        self.assertEqual(rebuilt["intent"]["recent_queries"][0]["uses"], 2)
        self.assertEqual(rebuilt["intent"]["recent_profiles"][0]["id"], "chainstream")
        self.assertEqual(rebuilt["intent"]["recent_profiles"][0]["uses"], 3)
        self.assertEqual(
            rebuilt["intent"]["persistent_query"]["query"],
            "multi-agent orchestration",
        )
        self.assertEqual(
            rebuilt["intent"]["persistent_query"]["profile_id"],
            "chainstream",
        )


class MediumWorkflowTests(AgentflowHomeTestCase):
    def test_medium_export_package_and_checklist_write_artifacts(self) -> None:
        article_id = "article_medium_ready"
        draft = DraftOutput(
            article_id=article_id,
            title="Medium packaging test",
            sections=[
                FilledSection(
                    heading="Intro",
                    content_markdown="This draft is ready for Medium import.",
                    word_count=8,
                    compliance_score=1.0,
                )
            ],
            total_word_count=8,
            image_placeholders=[
                ImagePlaceholder(
                    id=f"{article_id}_1",
                    description="hero chart",
                    section_heading="Intro",
                    resolved_path=str(self.home / "fixtures" / "hero.png"),
                )
            ],
        )
        save_draft(draft)

        draft_dir = self.home / "drafts" / article_id
        d3_output = D3Output(
            article_id=article_id,
            platform_versions=[
                PlatformVersion(
                    platform="medium",
                    content="# Medium packaging test\n\nMedium-specific body.\n",
                    metadata={
                        "title": "Medium packaging test",
                        "subtitle": "Medium subtitle",
                        "tags": ["medium", "agentflow"],
                        "canonical_url": None,
                    },
                    formatting_changes=["Adjusted paragraphs for Medium."],
                )
            ],
        )
        (draft_dir / "d3_output.json").write_text(
            json.dumps(d3_output.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        d4_storage.append_publish_record(
            article_id,
            PublishResult(
                platform="ghost_wordpress",
                status="success",
                published_url="https://ghost.example.com/post",
                platform_post_id="ghost_123",
            ),
        )
        review_state.transition(
            article_id,
            gate="B",
            to_state=review_state.STATE_DRAFT_PENDING_REVIEW,
            actor="test",
            decision="seed_review_state",
            force=True,
        )

        runner = CliRunner()

        export_result = runner.invoke(cli, ["medium-export", article_id, "--json"])
        self.assertEqual(export_result.exit_code, 0, export_result.output)
        export_payload = _parse_json_output(export_result.output)
        self.assertTrue(export_payload["medium_preview"]["available"])
        self.assertEqual(export_payload["images"]["resolved_count"], 1)
        self.assertTrue((self.home / "medium" / article_id / "export.json").exists())
        self.assertEqual(
            memory.read_memory_events(article_id=article_id)[-1]["event_type"],
            "medium_exported",
        )

        package_result = runner.invoke(
            cli,
            [
                "medium-package",
                article_id,
                "--distribution-mode",
                "cross_post",
                "--json",
            ],
        )
        self.assertEqual(package_result.exit_code, 0, package_result.output)
        package_payload = _parse_json_output(package_result.output)
        self.assertEqual(package_payload["source"], "medium_preview")
        self.assertEqual(
            package_payload["canonical_url"], "https://ghost.example.com/post"
        )
        self.assertTrue((self.home / "medium" / article_id / "package.json").exists())
        self.assertEqual(
            memory.read_memory_events(article_id=article_id)[-1]["event_type"],
            "medium_packaged",
        )
        self.assertEqual(
            review_state.current_state(article_id),
            review_state.STATE_READY_TO_PUBLISH,
        )
        gate_history = review_state.gate_history(article_id)
        self.assertEqual(gate_history[-1]["decision"], "medium_package_ready")
        self.assertEqual(len(gate_history), 2)

        checklist_result = runner.invoke(
            cli, ["medium-ops-checklist", article_id, "--json"]
        )
        self.assertEqual(checklist_result.exit_code, 0, checklist_result.output)
        checklist_payload = _parse_json_output(checklist_result.output)
        self.assertTrue(checklist_payload["ready_for_draft_import"])
        self.assertTrue(
            (self.home / "medium" / article_id / "ops_checklist.json").exists()
        )

    def test_medium_publish_without_token_generates_manual_package(self) -> None:
        article_id = "article_medium_manual_publish"
        draft = DraftOutput(
            article_id=article_id,
            title="Manual Medium publish",
            sections=[
                FilledSection(
                    heading="Intro",
                    content_markdown="This draft should become a Medium package.",
                    word_count=8,
                    compliance_score=1.0,
                )
            ],
            total_word_count=8,
            image_placeholders=[],
        )
        save_draft(draft)

        draft_dir = self.home / "drafts" / article_id
        d3_output = D3Output(
            article_id=article_id,
            platform_versions=[
                PlatformVersion(
                    platform="medium",
                    content="# Manual Medium publish\n\nMedium-ready body.\n",
                    metadata={
                        "title": "Manual Medium publish",
                        "subtitle": "Medium-ready body.",
                        "tags": ["medium"],
                    },
                )
            ],
        )
        (draft_dir / "d3_output.json").write_text(
            json.dumps(d3_output.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        review_state.transition(
            article_id,
            gate="B",
            to_state=review_state.STATE_DRAFT_PENDING_REVIEW,
            actor="test",
            decision="seed_review_state",
            force=True,
        )

        runner = CliRunner()
        with patch.dict(
            os.environ,
            {
                "MOCK_LLM": "",
                "MEDIUM_INTEGRATION_TOKEN": "",
            },
            clear=False,
        ):
            publish_result = runner.invoke(
                cli,
                ["publish", article_id, "--platforms", "medium", "--json"],
            )

        self.assertEqual(publish_result.exit_code, 0, publish_result.output)
        publish_payload = _parse_json_output(publish_result.output)
        self.assertEqual(len(publish_payload["results"]), 1)
        medium_result = publish_payload["results"][0]
        self.assertEqual(medium_result["platform"], "medium")
        self.assertEqual(medium_result["status"], "manual")
        self.assertNotEqual(medium_result["status"], "failed")
        self.assertIn("browser paste required", medium_result["failure_reason"])
        self.assertTrue(medium_result["raw_response"]["manual_required"])

        package_path = Path(medium_result["raw_response"]["package_path"])
        self.assertTrue(package_path.exists())
        self.assertTrue((self.home / "medium" / article_id / "package.json").exists())
        self.assertTrue((self.home / "medium" / article_id / "export.json").exists())

        history = d4_storage.read_publish_history(article_id)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["platform"], "medium")
        self.assertEqual(history[0]["status"], "manual")
        self.assertEqual(
            review_state.current_state(article_id),
            review_state.STATE_READY_TO_PUBLISH,
        )
        gate_history = review_state.gate_history(article_id)
        self.assertEqual(gate_history[-1]["decision"], "medium_package_ready")
        self.assertEqual(len(gate_history), 2)

    def test_dispatch_summary_treats_medium_manual_as_non_failed(self) -> None:
        text, retry_kb, retry_sid = review_render.render_dispatch_summary(
            article_id="article_medium_manual",
            results=[
                {
                    "platform": "medium",
                    "status": "manual",
                    "url": None,
                    "reason": "browser paste required",
                }
            ],
        )

        self.assertIn("medium", text)
        self.assertIn("browser paste required", text)
        self.assertEqual(retry_kb, {})
        self.assertIsNone(retry_sid)

    def test_dispatch_summary_registers_retry_for_failed_platforms(self) -> None:
        text, retry_kb, retry_sid = review_render.render_dispatch_summary(
            article_id="article_retry_failed",
            results=[
                {
                    "platform": "ghost_wordpress",
                    "status": "failed",
                    "url": None,
                    "reason": "timeout",
                }
            ],
        )

        self.assertIn("ghost\\_wordpress", text)
        self.assertIsNotNone(retry_sid)
        self.assertEqual(
            retry_kb["inline_keyboard"][0][0]["callback_data"],
            f"D:retry:{retry_sid}",
        )
        entry = review_short_id.resolve(retry_sid or "")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["gate"], "D")
        self.assertEqual(entry["article_id"], "article_retry_failed")
        self.assertEqual(entry["extra"]["failed"], ["ghost_wordpress"])

    def test_gate_d_retry_callback_spawns_failed_platforms(self) -> None:
        sid = review_short_id.register(
            gate="D",
            article_id="article_retry_callback",
            extra={"failed": ["ghost_wordpress"]},
        )
        entry = review_short_id.resolve(sid)
        self.assertIsNotNone(entry)

        with (
            patch("agentflow.agent_review.daemon.auth.is_authorized", return_value=True),
            patch("agentflow.agent_review.daemon.tg_client.answer_callback_query") as answer_mock,
            patch("agentflow.agent_review.daemon.tg_client.edit_message_reply_markup") as edit_mock,
            patch("agentflow.agent_review.daemon._spawn_publish_retry") as retry_mock,
        ):
            review_daemon._route(
                "D",
                "retry",
                sid,
                "",
                entry or {},
                "cb_retry",
                456,
                789,
                123,
            )

        answer_mock.assert_called_once_with(
            "cb_retry", text="🔁 重试中: ghost_wordpress"
        )
        edit_mock.assert_called_once_with(456, 789, reply_markup={})
        retry_mock.assert_called_once_with(
            "article_retry_callback", ["ghost_wordpress"]
        )
        self.assertIsNone(review_short_id.resolve(sid))

    def test_medium_package_falls_back_to_draft_and_flags_unresolved_images(self) -> None:
        article_id = "article_medium_fallback"
        draft = DraftOutput(
            article_id=article_id,
            title="Fallback Medium package",
            sections=[
                FilledSection(
                    heading="Intro",
                    content_markdown="[IMAGE: unresolved hero]\n\nFallback body content.",
                    word_count=6,
                    compliance_score=1.0,
                )
            ],
            total_word_count=6,
            image_placeholders=[
                ImagePlaceholder(
                    id=f"{article_id}_1",
                    description="unresolved hero",
                    section_heading="Intro",
                )
            ],
        )
        save_draft(draft)

        runner = CliRunner()
        package_result = runner.invoke(cli, ["medium-package", article_id, "--json"])
        self.assertEqual(package_result.exit_code, 0, package_result.output)
        package_payload = _parse_json_output(package_result.output)
        self.assertEqual(package_payload["source"], "draft_markdown")
        self.assertTrue(
            any("Missing Medium preview" in msg for msg in package_payload["warnings"])
        )

        checklist_result = runner.invoke(
            cli, ["medium-ops-checklist", article_id, "--json"]
        )
        self.assertEqual(checklist_result.exit_code, 0, checklist_result.output)
        checklist_payload = _parse_json_output(checklist_result.output)
        self.assertFalse(checklist_payload["ready_for_draft_import"])
        self.assertTrue(
            any("unresolved image placeholders" in item for item in checklist_payload["blockers"])
        )


class TgMenuV103Tests(AgentflowHomeTestCase):
    """v1.0.3 Telegram menu enrichment — orphan callback wiring, unified
    button labels, and the 8-command global slash menu."""

    def test_action_req_has_no_orphan_callbacks(self) -> None:
        """Every (gate, action) pair in ``_ACTION_REQ`` must be rendered as a
        callback button somewhere — either by ``render.py`` (cards) or by an
        inline keyboard inside ``daemon._route`` (follow-up notifications).

        This is the v1.0.3 acceptance gate: no callback is registered without
        a UI affordance to fire it."""
        from agentflow.agent_review import render as _render
        from pathlib import Path as _Path

        render_text = _Path(_render.__file__).read_text(encoding="utf-8")
        daemon_text = _Path(review_daemon.__file__).read_text(encoding="utf-8")
        haystack = render_text + "\n" + daemon_text

        orphans: list[str] = []
        for (gate, action) in review_daemon._ACTION_REQ.keys():
            needle = f"{gate}:{action}:"
            if needle not in haystack:
                orphans.append(needle)
        self.assertEqual(
            orphans,
            [],
            f"orphan callbacks found (registered in _ACTION_REQ but never "
            f"rendered): {orphans}",
        )

    def test_gate_b_card_uses_unified_label_set(self) -> None:
        text, kb, _sid = review_render.render_gate_b(
            article_id="article_label_b",
            title="Label test",
            subtitle=None,
            publisher_brand="brand",
            voice="first_party_brand",
            word_count=100,
            section_count=2,
            compliance_score=0.9,
            tags=[],
            self_check_lines=[],
            opening_excerpt="hello",
        )
        labels = {
            btn["text"]
            for row in kb["inline_keyboard"]
            for btn in row
        }
        callbacks = [
            btn["callback_data"]
            for row in kb["inline_keyboard"]
            for btn in row
        ]
        self.assertIn("✅ 通过", labels)
        self.assertIn("🚫 拒绝", labels)
        # Gate B v1.0.3 requirements: diff + defer wired as buttons
        self.assertTrue(any(cb.startswith("B:diff:") for cb in callbacks))
        self.assertTrue(
            any(cb.startswith("B:defer:") and "hours=2" in cb for cb in callbacks)
        )

    def test_gate_c_card_uses_unified_label_set_and_full_button(self) -> None:
        text, kb, _sid = review_render.render_gate_c(
            article_id="article_label_c",
            title="Cover label",
            image_mode="cover-only",
            cover_style="cover",
            cover_size="2048x1024",
            self_check_lines=[],
            brand_overlay_status="ON",
            brand_overlay_anchor="bottom_left",
            inline_body_count=0,
        )
        labels = {
            btn["text"]
            for row in kb["inline_keyboard"]
            for btn in row
        }
        callbacks = [
            btn["callback_data"]
            for row in kb["inline_keyboard"]
            for btn in row
        ]
        self.assertIn("✅ 通过", labels)
        self.assertIn("🚫 拒绝", labels)
        self.assertTrue(any(cb.startswith("C:full:") for cb in callbacks))
        self.assertTrue(
            any(cb.startswith("C:defer:") and "hours=2" in cb for cb in callbacks)
        )

    def test_gate_a_card_renders_expand_and_defer_buttons(self) -> None:
        text, kb, _sid = review_render.render_gate_a(
            publisher_brand="brand",
            target_series="A",
            candidates=[
                {"title": "T1", "angle": "a", "score": "0.5", "age_h": "1.0", "source": "s"},
            ],
            batch_path=str(self.home / "batch.json"),
        )
        callbacks = [
            btn["callback_data"]
            for row in kb["inline_keyboard"]
            for btn in row
        ]
        self.assertTrue(any(cb.startswith("A:expand:") for cb in callbacks))
        self.assertTrue(
            any(cb.startswith("A:defer:") and "hours=4" in cb for cb in callbacks)
        )

    def test_gate_d_confirm_uses_unified_pass_label(self) -> None:
        sid = review_short_id.register(gate="D", article_id="art_d_label")
        text, kb = review_render.render_gate_d(
            article_id="art_d_label",
            title="Channel select label",
            available=["medium", "twitter"],
            selected=set(),
            short_id=sid,
        )
        labels = {
            btn["text"]
            for row in kb["inline_keyboard"]
            for btn in row
        }
        self.assertIn("✅ 通过", labels)
        self.assertIn("🚫 拒绝", labels)

    def test_defer_callback_schedules_repost_via_store(self) -> None:
        self.stack.enter_context(
            patch.object(review_daemon, "_REVIEW_HOME", self.home / "review")
        )
        sid = review_short_id.register(gate="B", article_id="article_defer_b")
        entry = review_short_id.resolve(sid) or {}
        with (
            patch("agentflow.agent_review.daemon.auth.is_authorized", return_value=True),
            patch("agentflow.agent_review.daemon.tg_client.answer_callback_query") as ack_mock,
            patch("agentflow.agent_review.daemon.tg_client.edit_message_reply_markup"),
            patch("agentflow.agent_review.daemon.tg_client.send_message"),
        ):
            review_daemon._route(
                "B",
                "defer",
                sid,
                "hours=2",
                entry,
                "cb1",
                456,
                789,
                123,
            )
        ack_mock.assert_called_once()
        scheduled = json.loads(
            (self.home / "review" / "deferred_reposts.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(len(scheduled), 1)
        self.assertEqual(scheduled[0]["gate"], "B")
        self.assertEqual(scheduled[0]["article_id"], "article_defer_b")
        self.assertAlmostEqual(scheduled[0]["hours"], 2.0)
        self.assertIn("due_at", scheduled[0])

    def test_eight_v103_slash_commands_have_handlers(self) -> None:
        """Each of the v1.0.3 commands must be wired as an entry inside
        ``_handle_message`` AND advertised in ``_BOT_COMMANDS``."""
        import inspect as _inspect

        source = _inspect.getsource(review_daemon._handle_message)
        v103_cmds = [
            "/status", "/queue", "/help", "/skip", "/defer",
            "/publish-mark", "/audit", "/auth-debug",
        ]
        for cmd in v103_cmds:
            self.assertIn(
                cmd, source, f"slash command {cmd!r} missing from _handle_message",
            )
        # Help text must regenerate the role matrix from _ACTION_REQ.
        help_text = review_daemon._build_help_text()
        self.assertIn("Role Matrix", help_text)
        for (gate, action) in review_daemon._ACTION_REQ.keys():
            # MarkdownV2 escapes ``_`` so match the escaped form too.
            needle = f"{gate}:{action.replace('_', chr(92) + '_')}"
            self.assertIn(needle, help_text)
        # Bot commands surface should also include the new menu entries.
        cmd_names = {item["command"] for item in review_daemon._BOT_COMMANDS}
        self.assertTrue(
            {"status", "queue", "skip", "defer", "audit", "auth_debug"}
            <= cmd_names
        )

    def test_auth_debug_returns_non_empty_role_matrix(self) -> None:
        sent: list[tuple[Any, ...]] = []

        def _capture(*args: Any, **kwargs: Any) -> None:
            sent.append((args, kwargs))

        with (
            patch("agentflow.agent_review.daemon.auth.is_authorized", return_value=True),
            patch(
                "agentflow.agent_review.daemon.tg_client.send_message",
                side_effect=_capture,
            ),
        ):
            review_daemon._send_auth_debug(chat_id=456, uid=123)

        self.assertEqual(len(sent), 1)
        body = sent[0][0][1]
        self.assertIn("Auth Debug", body)
        # Role matrix line is rendered for every (gate, action) pair —
        # MarkdownV2 escapes ``_`` so we check the escaped form.
        for (gate, action) in review_daemon._ACTION_REQ.keys():
            needle = f"{gate}:{action.replace('_', chr(92) + '_')}"
            self.assertIn(needle, body)

    def test_a_expand_callback_sends_expanded_batch(self) -> None:
        batch_path = self.home / "hotspots_expand.json"
        batch_path.write_text(
            json.dumps({
                "hotspots": [
                    {
                        "id": "hs_x1",
                        "topic_one_liner": "Test topic A",
                        "suggested_angles": [
                            {"title": "Angle one", "angle": "a"},
                        ],
                        "source_references": [
                            {"text_snippet": "Some signal text"},
                        ],
                        "freshness_score": 0.7,
                        "depth_potential": "medium",
                    }
                ]
            }),
            encoding="utf-8",
        )
        sid = review_short_id.register(gate="A", batch_path=str(batch_path))
        entry = review_short_id.resolve(sid) or {}

        with (
            patch("agentflow.agent_review.daemon.auth.is_authorized", return_value=True),
            patch("agentflow.agent_review.daemon.tg_client.answer_callback_query") as ack_mock,
            patch("agentflow.agent_review.daemon.tg_client.send_long_text") as send_mock,
        ):
            review_daemon._route(
                "A", "expand", sid, "", entry, "cb1", 456, 789, 123,
            )
        ack_mock.assert_called_once()
        send_mock.assert_called_once()
        body = send_mock.call_args.args[1]
        self.assertIn("Test topic A", body)
        self.assertIn("Angle one", body)


class TopicSpineLintTests(AgentflowHomeTestCase):
    """v1.0.21 — catches drafts where the LLM forced-grafts publisher
    tokens onto an off-topic hotspot (e.g. TCG customs article spun
    around an on-chain data infra publisher). Different signal from
    specificity_lint: spine_lint compares SOURCE material vs publisher
    DOMAIN, not draft body vs publisher BRAND."""

    PUBLISHER = {
        "brand": "ChainStream",
        "default_description": "AI-native crypto infra real-time on-chain data",
        "product_facts": [
            "ChainStream uses Kafka Streams to ingest on-chain events",
            "MCP execution layer for smart-money agents",
            "Sub-second latency on Solana mainnet",
        ],
        "perspectives": [
            "Most crypto data infra is batch — we are streaming-first",
            "Agents need on-chain data as state machine input, not a chart",
        ],
    }

    def test_aligned_topic_passes(self) -> None:
        from agentflow.agent_d2.topic_spine_lint import detect_topic_spine_misalignment
        meta = {
            "topic_one_liner": "Solana 上 smart-money agent 实时套利",
            "source_references": [
                {"text_snippet": "Kafka-based on-chain data infra outage cascading into agent execution"},
                {"text_snippet": "Sub-second on-chain event ingestion benchmark vs batch ETL"},
            ],
        }
        warn = detect_topic_spine_misalignment(meta, self.PUBLISHER)
        self.assertIsNone(warn)

    def test_off_topic_TCG_customs_flagged(self) -> None:
        """The exact failure mode autopost reported: customs article
        with publisher tokens grafted in via D2 prompt anchoring."""
        from agentflow.agent_d2.topic_spine_lint import detect_topic_spine_misalignment
        meta = {
            "topic_one_liner": "保税仓省税 20% 不稀奇,清关单证错一次全赔进去",
            "source_references": [
                {"text_snippet": "鹿特丹港海关锁柜 TCG 卡牌 滞港费 报关单证 HS 编码 原产地证 进口增值税"},
                {"text_snippet": "荷兰 AEO 认证 跨境电商 保税仓 转口报关 滞纳金 销毁费用"},
                {"text_snippet": "供应商发票 字段截断 ERP 导出 物流单 Bill of Lading 货代"},
            ],
        }
        warn = detect_topic_spine_misalignment(meta, self.PUBLISHER)
        self.assertIsNotNone(warn)
        self.assertIn("topic-spine misalignment", warn)
        self.assertIn("强行嫁接", warn)

    def test_thin_spine_skipped(self) -> None:
        from agentflow.agent_d2.topic_spine_lint import detect_topic_spine_misalignment
        meta = {"topic_one_liner": "x", "source_references": []}
        self.assertIsNone(detect_topic_spine_misalignment(meta, self.PUBLISHER))

    def test_thin_publisher_skipped(self) -> None:
        from agentflow.agent_d2.topic_spine_lint import detect_topic_spine_misalignment
        meta = {
            "topic_one_liner": "long topic line that has plenty of tokens",
            "source_references": [
                {"text_snippet": "fully fledged source snippet about something"},
            ],
        }
        self.assertIsNone(detect_topic_spine_misalignment(
            meta, {"brand": "x"},  # too thin to lint
        ))


class TopicFitHardGateTests(AgentflowHomeTestCase):
    """v1.0.21 — `AGENTFLOW_TOPIC_FIT_HARD_THRESHOLD` gates D1 hotspots
    before D2 reads them, instead of just down-ranking via composite
    score. Default 0 keeps v1.0.20 behavior; setting > 0 enables a hard
    drop for prod brand discipline."""

    def test_hard_threshold_drops_low_fit_hotspots(self) -> None:
        from agentflow.agent_review import triggers
        from agentflow.shared.bootstrap import agentflow_home
        # Stub TG so post_gate_a doesn't try to send. We only care about
        # the filter behavior + return value.
        with (
            patch.dict(
                os.environ,
                {
                    "AGENTFLOW_TOPIC_FIT_HARD_THRESHOLD": "0.10",
                    "AGENTFLOW_FIT_WEIGHT": "0.6",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "TELEGRAM_REVIEW_CHAT_ID": "456",
                },
                clear=False,
            ),
            patch(
                "agentflow.agent_review.triggers.tg_client.send_message",
                return_value={"message_id": 99},
            ),
            patch(
                "agentflow.agent_review.triggers._daemon.get_review_chat_id",
                return_value=456,
            ),
            patch(
                "agentflow.agent_review.triggers._sid.find_active",
                return_value=None,
            ),
        ):
            # Two hotspots — one aligned, one off-domain.
            aligned = {
                "id": "h1",
                "topic_one_liner": "Kafka Streams on-chain event ingestion",
                "source_references": [{"text_snippet": "Solana smart money MCP"}],
                "freshness_score": 0.5,
            }
            off_domain = {
                "id": "h2",
                "topic_one_liner": "TCG 卡牌 荷兰保税仓 报关单证错一位",
                "source_references": [{"text_snippet": "鹿特丹港 滞港费 HS 编码"}],
                "freshness_score": 0.9,
            }
            publisher = {
                "brand": "ChainStream",
                "product_facts": [
                    "ChainStream uses Kafka Streams to ingest on-chain events",
                    "MCP for smart-money agents",
                    "Sub-second latency on Solana",
                ],
                "default_tags": ["onchain", "agent", "infra"],
            }
            # Resolve the "from agentflow.agent_d1.topic_fit import score_fit"
            # naturally; no mock — we want the real score.
            (agentflow_home() / "hotspots").mkdir(parents=True, exist_ok=True)
            (agentflow_home() / "hotspots" / "today.json").write_text("{}", encoding="utf-8")
            res = triggers.post_gate_a(
                hotspots=[aligned, off_domain],
                batch_path=str(agentflow_home() / "hotspots" / "today.json"),
                publisher_brand="ChainStream",
                top_k=3,
                publisher_account=publisher,
            )
        self.assertIsNotNone(res)
        # Off-domain hotspot should have been dropped pre-rank.
        self.assertEqual(res.get("candidate_count"), 1)

    def test_hard_threshold_zero_preserves_legacy_behavior(self) -> None:
        from agentflow.agent_review import triggers
        from agentflow.shared.bootstrap import agentflow_home
        with (
            patch.dict(
                os.environ,
                {
                    "AGENTFLOW_TOPIC_FIT_HARD_THRESHOLD": "0",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "TELEGRAM_REVIEW_CHAT_ID": "456",
                },
                clear=False,
            ),
            patch(
                "agentflow.agent_review.triggers.tg_client.send_message",
                return_value={"message_id": 99},
            ),
            patch(
                "agentflow.agent_review.triggers._daemon.get_review_chat_id",
                return_value=456,
            ),
            patch(
                "agentflow.agent_review.triggers._sid.find_active",
                return_value=None,
            ),
        ):
            (agentflow_home() / "hotspots").mkdir(parents=True, exist_ok=True)
            batch = agentflow_home() / "hotspots" / "today.json"
            batch.write_text("{}", encoding="utf-8")
            off_domain = {
                "id": "h2",
                "topic_one_liner": "TCG 保税仓 报关",
                "source_references": [{"text_snippet": "鹿特丹"}],
                "freshness_score": 0.9,
            }
            publisher = {
                "brand": "ChainStream",
                "product_facts": ["Kafka", "MCP", "Solana"],
            }
            res = triggers.post_gate_a(
                hotspots=[off_domain], batch_path=str(batch),
                publisher_brand="ChainStream", top_k=3,
                publisher_account=publisher,
            )
        self.assertIsNotNone(res)
        # With hard threshold 0, off-domain hotspot still surfaces (just
        # downranked via composite). Backward compat preserved.
        self.assertEqual(res.get("candidate_count"), 1)


class LarkWebhookTests(AgentflowHomeTestCase):
    """v1.0.19 path A — outbound Lark Custom Bot fan-out. HITL stays
    on Telegram; Lark gets digest / dispatch / publish-ready / spawn
    failure summaries. Push-only, never callbacks back."""

    def test_noop_when_url_unset(self) -> None:
        from agentflow.shared import lark_webhook
        # No URL configured → all entry points return without hitting
        # the network at all (requests is lazy-imported inside _post,
        # so a no-op shouldn't reach that import).
        with patch.dict(os.environ, {"LARK_WEBHOOK_URL": ""}, clear=False):
            # If any of these tried to POST, the absence of LARK_WEBHOOK_URL
            # would surface as an exception inside requests; we just call
            # them and rely on the early-return semantics.
            lark_webhook.send_text("hello")
            lark_webhook.send_card(title="t", body_md="b")
            lark_webhook.notify_dispatch_result(
                article_id="a", title="t", succeeded=["medium"], failed=[],
            )
            lark_webhook.notify_publish_ready(article_id="a", title="t")
            lark_webhook.notify_hotspots_digest(scan_count=0, top_titles=[])
            lark_webhook.notify_spawn_failure(
                label="x", target_id="y", error_tail="z",
            )

    def test_sign_matches_lark_spec(self) -> None:
        """Per Lark docs: HmacSHA256(stringToSign='timestamp\\n'+secret, body=b'')
        then base64."""
        from agentflow.shared import lark_webhook
        # Reference value calculated independently with the same algorithm:
        sig = lark_webhook._sign(100, "demo")
        self.assertIsInstance(sig, str)
        self.assertGreater(len(sig), 16)
        # Re-running gives the same value (pure function).
        self.assertEqual(sig, lark_webhook._sign(100, "demo"))
        # Different secret → different sig.
        self.assertNotEqual(sig, lark_webhook._sign(100, "other"))

    def test_send_text_includes_sign_when_secret_set(self) -> None:
        from agentflow.shared import lark_webhook

        captured: list[dict[str, Any]] = []

        class _Resp:
            ok = True
            status_code = 200
            text = '{"code":0}'
            def json(self): return {"code": 0}

        def _fake_post(url, json=None, timeout=10, **_):
            captured.append({"url": url, "json": json})
            return _Resp()

        with (
            patch.dict(
                os.environ,
                {
                    "LARK_WEBHOOK_URL": "https://example.com/hook/abc",
                    "LARK_WEBHOOK_SECRET": "demo",
                    "LARK_WEBHOOK_NO_DEFER": "true",
                    "LARK_WEBHOOK_KEYWORDS": "",
                },
                clear=False,
            ),
            patch("requests.post", side_effect=_fake_post),
        ):
            lark_webhook.send_text("hello world")

        self.assertEqual(len(captured), 1)
        body = captured[0]["json"]
        self.assertEqual(body["msg_type"], "text")
        self.assertIn("timestamp", body)
        self.assertIn("sign", body)
        # sign should match what _sign() computes for the same ts+secret.
        expected = lark_webhook._sign(int(body["timestamp"]), "demo")
        self.assertEqual(body["sign"], expected)

    def test_keyword_appended_when_missing(self) -> None:
        from agentflow.shared import lark_webhook
        # The text doesn't contain the keyword, helper should append it.
        out = lark_webhook._ensure_keyword(
            "ordinary message body",
            ["AgentFlow", "alert"],
        )
        self.assertIn("[AgentFlow]", out)
        # Already present → unchanged.
        out2 = lark_webhook._ensure_keyword(
            "AgentFlow ran a scan",
            ["AgentFlow"],
        )
        self.assertEqual(out2, "AgentFlow ran a scan")

    def test_truncate_caps_oversized_payload(self) -> None:
        from agentflow.shared import lark_webhook
        big = {"msg_type": "text", "content": {"text": "x" * 30_000}}
        out = lark_webhook._truncate(big)
        raw = json.dumps(out, ensure_ascii=False).encode("utf-8")
        self.assertLessEqual(len(raw), lark_webhook._BODY_HARD_CAP_BYTES)
        self.assertIn("truncated", out["content"]["text"])

    def test_in_rate_limit_zone_detects_half_hours(self) -> None:
        from agentflow.shared import lark_webhook
        from datetime import datetime
        # 10:00:30 — within the ±60s zone.
        self.assertTrue(lark_webhook._in_rate_limit_zone(
            datetime(2026, 5, 3, 10, 0, 30)
        ))
        # 10:30:00 — exactly half-hour, zone.
        self.assertTrue(lark_webhook._in_rate_limit_zone(
            datetime(2026, 5, 3, 10, 30, 0)
        ))
        # 10:15:00 — middle of half, safe.
        self.assertFalse(lark_webhook._in_rate_limit_zone(
            datetime(2026, 5, 3, 10, 15, 0)
        ))

    # v1.0.20 polish ---------------------------------------------------

    def _capture_lark_payloads(self, fn) -> list[dict[str, Any]]:
        """Capture the JSON body that lark_webhook would POST."""
        captured: list[dict[str, Any]] = []

        class _Resp:
            ok = True
            status_code = 200
            text = '{"code":0}'
            def json(self): return {"code": 0}

        def _fake(url, json=None, timeout=10, **_):
            captured.append(json)
            return _Resp()

        with patch("requests.post", side_effect=_fake):
            fn()
        return captured

    def test_brand_prefix_prepended_to_title(self) -> None:
        from agentflow.shared import lark_webhook
        with patch.dict(
            os.environ,
            {
                "LARK_WEBHOOK_URL": "https://example.com/hook/abc",
                "LARK_WEBHOOK_NO_DEFER": "true",
                "LARK_WEBHOOK_BRAND_PREFIX": "[ChainStream]",
                "LARK_WEBHOOK_KEYWORDS": "",
                "LARK_WEBHOOK_SECRET": "",
                "LARK_WEBHOOK_TG_BOT_URL": "",
                "LARK_WEBHOOK_DASHBOARD_URL_TEMPLATE": "",
            },
            clear=False,
        ):
            payloads = self._capture_lark_payloads(
                lambda: lark_webhook.send_card(title="🔎 hi", body_md="x")
            )
        self.assertEqual(len(payloads), 1)
        title = payloads[0]["card"]["header"]["title"]["content"]
        self.assertTrue(title.startswith("[ChainStream] 🔎 hi"))

    def test_actionable_cards_include_tg_button(self) -> None:
        from agentflow.shared import lark_webhook
        env = {
            "LARK_WEBHOOK_URL": "https://example.com/hook/abc",
            "LARK_WEBHOOK_NO_DEFER": "true",
            "LARK_WEBHOOK_TG_BOT_URL": "https://t.me/CSPostContentAuditBot",
            "LARK_WEBHOOK_BRAND_PREFIX": "",
            "LARK_WEBHOOK_KEYWORDS": "",
            "LARK_WEBHOOK_SECRET": "",
            "LARK_WEBHOOK_DASHBOARD_URL_TEMPLATE": "",
        }
        with patch.dict(os.environ, env, clear=False):
            # publish_ready → must show "去 TG 标记"
            p1 = self._capture_lark_payloads(lambda: lark_webhook.notify_publish_ready(
                article_id="aid1", title="t",
            ))
            # dispatch with failures → must show "去 TG 重试"
            p2 = self._capture_lark_payloads(lambda: lark_webhook.notify_dispatch_result(
                article_id="aid1", title="t",
                succeeded=["medium"],
                failed=[("twitter_thread", "rate limit")],
            ))
            # dispatch full success → no TG button needed (informational)
            p3 = self._capture_lark_payloads(lambda: lark_webhook.notify_dispatch_result(
                article_id="aid1", title="t",
                succeeded=["medium", "ghost_wordpress"],
                failed=[],
            ))

        def _button_urls(payload):
            actions: list[str] = []
            for el in payload["card"]["elements"]:
                if el.get("tag") == "action":
                    for a in el["actions"]:
                        actions.append(a["url"])
            return actions

        self.assertIn("https://t.me/CSPostContentAuditBot", _button_urls(p1[0]))
        self.assertIn("https://t.me/CSPostContentAuditBot", _button_urls(p2[0]))
        self.assertNotIn(
            "https://t.me/CSPostContentAuditBot", _button_urls(p3[0]),
            "full-success card shouldn't show 'go to TG' button",
        )

    def test_dashboard_url_template_renders(self) -> None:
        from agentflow.shared import lark_webhook
        env = {
            "LARK_WEBHOOK_URL": "https://example.com/hook/abc",
            "LARK_WEBHOOK_NO_DEFER": "true",
            "LARK_WEBHOOK_TG_BOT_URL": "",
            "LARK_WEBHOOK_BRAND_PREFIX": "",
            "LARK_WEBHOOK_KEYWORDS": "",
            "LARK_WEBHOOK_SECRET": "",
            "LARK_WEBHOOK_DASHBOARD_URL_TEMPLATE":
                "https://dash.example.com/article/{article_id}",
        }
        with patch.dict(os.environ, env, clear=False):
            payloads = self._capture_lark_payloads(
                lambda: lark_webhook.notify_publish_ready(
                    article_id="hs_001", title="t",
                )
            )
        urls: list[str] = []
        for el in payloads[0]["card"]["elements"]:
            if el.get("tag") == "action":
                for a in el["actions"]:
                    urls.append(a["url"])
        self.assertIn("https://dash.example.com/article/hs_001", urls)

    def test_reason_maxlen_caps_long_reasons(self) -> None:
        from agentflow.shared import lark_webhook
        env = {
            "LARK_WEBHOOK_URL": "https://example.com/hook/abc",
            "LARK_WEBHOOK_NO_DEFER": "true",
            "LARK_WEBHOOK_REASON_MAXLEN": "30",
            "LARK_WEBHOOK_TG_BOT_URL": "",
            "LARK_WEBHOOK_BRAND_PREFIX": "",
            "LARK_WEBHOOK_KEYWORDS": "",
            "LARK_WEBHOOK_SECRET": "",
            "LARK_WEBHOOK_DASHBOARD_URL_TEMPLATE": "",
        }
        long_reason = "x" * 200
        with patch.dict(os.environ, env, clear=False):
            payloads = self._capture_lark_payloads(
                lambda: lark_webhook.notify_dispatch_result(
                    article_id="aid", title="t", succeeded=[],
                    failed=[("plat_x", long_reason)],
                )
            )
        body = payloads[0]["card"]["elements"][0]["text"]["content"]
        self.assertNotIn("x" * 31, body)
        self.assertIn("…", body)


class SpecificityLintTests(AgentflowHomeTestCase):
    """v1.0.18 — anchoring lint catches drafts that "sound specific"
    (have product names / dates / numbers) but those names are generic
    AI/Web3 lingo, not the publisher's own brand assets."""

    PROFILE = {
        "brand": "ChainStream",
        "default_description": "AI-native crypto infra for real-time on-chain data",
        "product_facts": [
            "ChainStream uses Kafka Streams to ingest on-chain events",
            "MCP execution layer for smart-money agents",
            "Sub-second latency on Solana mainnet",
        ],
        "perspectives": [
            "Most crypto data infra is batch — we are streaming-first",
            "Agents need on-chain data as a state machine input, not a chart",
        ],
    }

    def test_anchored_draft_passes(self) -> None:
        from agentflow.agent_d2.specificity_lint import detect_specificity_drift
        sections = [
            {"content_markdown": (
                "ChainStream 跑在 Solana 主网,Kafka Streams 是底层管道. "
                "我们的 MCP 执行层把 on-chain 数据当作 agent 的输入."
            )},
            {"content_markdown": (
                "smart-money 这种场景对 streaming-first 的诉求最强. "
                "我们的看法是 agents 把数据当 state machine input 用."
            )},
        ]
        warn = detect_specificity_drift(sections, self.PROFILE)
        self.assertIsNone(warn)

    def test_generic_draft_flagged(self) -> None:
        from agentflow.agent_d2.specificity_lint import detect_specificity_drift
        sections = [
            {"content_markdown": (
                "AI Agent 的发展经历了几个阶段. 主流框架在 2024 年后才真正起步."
                "many startups have explored this space."
            )},
            {"content_markdown": (
                "Web3 数据基础设施面临挑战. 行业还在早期阶段, 没有共识."
            )},
            {"content_markdown": (
                "未来几年的趋势是 agent 与 infra 的融合, 哪家先做出标准化谁就赢."
            )},
        ]
        warn = detect_specificity_drift(sections, self.PROFILE)
        self.assertIsNotNone(warn)
        self.assertIn("specificity drift", warn)

    def test_no_publisher_returns_none(self) -> None:
        from agentflow.agent_d2.specificity_lint import detect_specificity_drift
        warn = detect_specificity_drift(
            [{"content_markdown": "any text"}], None,
        )
        self.assertIsNone(warn)

    def test_thin_profile_skips_lint(self) -> None:
        """Profile with < 5 anchor tokens can't reliably lint; doctor
        probe handles the upstream warn separately."""
        from agentflow.agent_d2.specificity_lint import detect_specificity_drift
        warn = detect_specificity_drift(
            [{"content_markdown": "any text here"}],
            {"brand": "X"},  # only one short token
        )
        self.assertIsNone(warn)


class ActiveProfileThinnessTests(AgentflowHomeTestCase):
    """v1.0.18 — `af doctor` surfaces a thin active profile (< 3 facts
    or < 2 perspectives) before drafts are written."""

    def test_thin_profile_warns(self) -> None:
        from agentflow.agent_review import preflight
        with (
            patch(
                "agentflow.cli.topic_profile_commands._read_active_profile_id",
                return_value="brandX",
            ),
            patch(
                "agentflow.shared.topic_profile_lifecycle.load_user_topic_profiles",
                return_value={
                    "profiles": {
                        "brandX": {
                            "publisher_account": {
                                "product_facts": ["only one"],
                                "perspectives": [],
                            }
                        }
                    }
                },
            ),
        ):
            cr = preflight.check_active_profile_thinness()
        self.assertFalse(cr.ok)
        self.assertIn("product_facts=1", cr.message)
        self.assertIn("perspectives=0", cr.message)

    def test_rich_profile_passes(self) -> None:
        from agentflow.agent_review import preflight
        with (
            patch(
                "agentflow.cli.topic_profile_commands._read_active_profile_id",
                return_value="brandY",
            ),
            patch(
                "agentflow.shared.topic_profile_lifecycle.load_user_topic_profiles",
                return_value={
                    "profiles": {
                        "brandY": {
                            "publisher_account": {
                                "product_facts": ["a", "b", "c"],
                                "perspectives": ["x", "y"],
                            }
                        }
                    }
                },
            ),
        ):
            cr = preflight.check_active_profile_thinness()
        self.assertTrue(cr.ok)
        self.assertIn("3 facts / 2 perspectives", cr.message)


class HotspotsScheduleTests(AgentflowHomeTestCase):
    """v1.0.17 — daemon-internal cross-OS hotspots scheduler. Replaces
    the macOS-only `af review-cron-install` for Linux / Docker / sandbox
    deployments where launchctl was a no-op."""

    def test_parse_schedule_drops_bad_slots(self) -> None:
        from agentflow.agent_review import schedule
        self.assertEqual(
            schedule._parse_schedule("09:00, 18:00 ,bad,25:00,18:60"),
            [(9, 0), (18, 0)],
        )
        self.assertEqual(schedule._parse_schedule(""), [])
        self.assertEqual(schedule._parse_schedule(None), [])

    def test_slot_due_only_after_slot_today_unfired(self) -> None:
        from agentflow.agent_review import schedule
        slot = (9, 0)
        # 09:00:30 same day → due (within 90s window).
        now = datetime(2026, 5, 2, 9, 0, 30, tzinfo=timezone.utc)
        self.assertTrue(schedule._slot_due(slot, now, last_fired=None))
        # 08:59 → not yet due.
        early = datetime(2026, 5, 2, 8, 59, tzinfo=timezone.utc)
        self.assertFalse(schedule._slot_due(slot, early, last_fired=None))
        # 09:30 → outside the 90s window; missed for the day.
        late = datetime(2026, 5, 2, 9, 30, tzinfo=timezone.utc)
        self.assertFalse(schedule._slot_due(slot, late, last_fired=None))
        # 09:00:30 but already fired today → not due.
        fired_today = datetime(2026, 5, 2, 9, 0, 5, tzinfo=timezone.utc)
        self.assertFalse(schedule._slot_due(slot, now, last_fired=fired_today))
        # 09:00:30 today, last fire was yesterday → due.
        fired_yesterday = datetime(2026, 5, 1, 9, 0, 5, tzinfo=timezone.utc)
        self.assertTrue(schedule._slot_due(slot, now, last_fired=fired_yesterday))

    def test_due_slots_returns_only_unfired(self) -> None:
        from agentflow.agent_review import schedule
        now = datetime(2026, 5, 2, 9, 0, 30, tzinfo=timezone.utc)
        state = {"18:00": "2026-05-01T18:00:10+00:00"}
        due = schedule.due_slots(
            schedule=[(9, 0), (18, 0)], now=now, state=state,
        )
        self.assertEqual(due, [(9, 0)])

    def test_fire_due_calls_spawn_and_stamps_state(self) -> None:
        from agentflow.agent_review import schedule
        spawn_calls: list[int] = []

        def _fake_spawn(top_k: int) -> None:
            spawn_calls.append(top_k)

        # Force the wall-clock-driven helpers to see our test slot as due.
        slot = (datetime.now().astimezone().hour,
                datetime.now().astimezone().minute)
        with patch.dict(
            os.environ,
            {
                "AGENTFLOW_HOTSPOTS_SCHEDULE": f"{slot[0]:02d}:{slot[1]:02d}",
                "AGENTFLOW_HOTSPOTS_SCHEDULE_TOP_K": "5",
            },
            clear=False,
        ):
            fired = schedule.fire_due(_fake_spawn)
        self.assertEqual(len(spawn_calls), 1)
        self.assertEqual(spawn_calls[0], 5)
        self.assertEqual(len(fired), 1)
        # Re-firing in the same window should be idempotent (state stamps).
        with patch.dict(
            os.environ,
            {
                "AGENTFLOW_HOTSPOTS_SCHEDULE": f"{slot[0]:02d}:{slot[1]:02d}",
                "AGENTFLOW_HOTSPOTS_SCHEDULE_TOP_K": "5",
            },
            clear=False,
        ):
            fired_again = schedule.fire_due(_fake_spawn)
        self.assertEqual(len(spawn_calls), 1, "should not re-fire same slot")
        self.assertEqual(fired_again, [])

    def test_status_disabled_when_env_empty(self) -> None:
        from agentflow.agent_review import schedule
        with patch.dict(os.environ, {"AGENTFLOW_HOTSPOTS_SCHEDULE": ""}, clear=False):
            snap = schedule.status()
        self.assertFalse(snap["enabled"])
        self.assertEqual(snap["slots"], [])

    def test_cron_install_refuses_on_non_darwin(self) -> None:
        from click.testing import CliRunner
        from agentflow.cli.commands import cli
        with patch("platform.system", return_value="Linux"):
            res = CliRunner().invoke(cli, ["review-cron-install"])
        self.assertNotEqual(res.exit_code, 0)
        self.assertIn("AGENTFLOW_HOTSPOTS_SCHEDULE", res.output)


class V016BatchTests(AgentflowHomeTestCase):
    """v1.0.16 — five operator-feedback fixes batched: token-aware
    preflight cache (#1a), image compression for sendPhoto (#5), stale
    Gate card keyboard cleanup (#4a), profile-snapshot outdated flag
    (#2a), language consistency lint (#3b)."""

    # ---- #1a: token-aware preflight cache ---------------------------
    def test_cache_invalidates_on_token_change(self) -> None:
        from agentflow.agent_review import preflight
        preflight._cache_put(
            "telegram", True, "valid (@old_bot)", token_fp="aaaa1111",
        )
        # Same fingerprint → hit.
        hit = preflight._cache_get("telegram", token_fp="aaaa1111")
        self.assertIsNotNone(hit)
        # Different fingerprint → miss.
        miss = preflight._cache_get("telegram", token_fp="bbbb2222")
        self.assertIsNone(miss)

    def test_cache_pre_v016_entry_treated_as_miss(self) -> None:
        from agentflow.agent_review import preflight
        # Manually plant a pre-v016 cache entry (no token_fp key).
        cache = {
            "telegram": {
                "ts": __import__("time").time(),
                "valid": False,
                "message": "HTTP 401",
                "extra": {},
            }
        }
        preflight._write_cache(cache)
        miss = preflight._cache_get("telegram", token_fp="aaaa1111")
        self.assertIsNone(miss)

    # ---- #5: photo optimize + fallback -----------------------------
    def test_small_photo_passes_through_unmodified(self) -> None:
        from agentflow.agent_review import tg_client
        small = self.home / "small.png"
        small.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        out = tg_client._optimize_photo_for_telegram(small)
        self.assertEqual(out, small)

    def test_large_photo_gets_optimized(self) -> None:
        import os as _os
        from PIL import Image
        from agentflow.agent_review import tg_client
        big = self.home / "big.png"
        # High-entropy 2400x1600 noise so PNG can't compress below 1MB.
        rng = _os.urandom(2400 * 1600 * 4)
        Image.frombytes("RGBA", (2400, 1600), rng).save(
            big, "PNG", compress_level=0,
        )
        self.assertGreater(big.stat().st_size, 1_000_000)
        out = tg_client._optimize_photo_for_telegram(big)
        self.assertNotEqual(out, big)
        self.assertTrue(out.exists())
        self.assertLess(out.stat().st_size, big.stat().st_size)
        # Long edge clamped.
        with Image.open(out) as im:
            self.assertLessEqual(max(im.size), 1600)

    def test_send_photo_falls_back_to_send_document_on_timeout(self) -> None:
        import requests
        from agentflow.agent_review import tg_client
        photo = self.home / "p.png"
        photo.write_bytes(b"\x89PNG" + b"\x00" * 50)
        with (
            patch(
                "agentflow.agent_review.tg_client._post_multipart",
                side_effect=requests.exceptions.Timeout("upload timed out"),
            ),
            patch(
                "agentflow.agent_review.tg_client.send_document",
                return_value={"message_id": 99},
            ) as doc_mock,
        ):
            result = tg_client.send_photo(456, photo)
        doc_mock.assert_called_once()
        self.assertEqual(result["message_id"], 99)

    # ---- #4a: stale Gate card keyboard cleanup ---------------------
    def test_revoke_prior_card_keyboard_clears_old_message(self) -> None:
        from agentflow.agent_review import triggers, short_id as _sid
        sid = _sid.register(
            gate="B", article_id="hs_x_001", ttl_hours=24,
        )
        # v1.0.16: tg_message_id is stamped via the attach setter, not
        # at register time. Simulate the post-send flow.
        self.assertTrue(_sid.attach_message_id(sid, 42))
        with patch(
            "agentflow.agent_review.triggers.tg_client.edit_message_reply_markup",
        ) as edit_mock:
            triggers._revoke_prior_card_keyboard("B", "hs_x_001", 456)
        edit_mock.assert_called_once()
        args = edit_mock.call_args
        self.assertEqual(args.args[0], 456)
        self.assertEqual(args.args[1], 42)
        # Old sid revoked.
        self.assertIsNone(_sid.resolve(sid))

    # ---- #2a: profile snapshot + outdated flag ---------------------
    def test_language_lint_flags_zh_with_too_much_ascii(self) -> None:
        from agentflow.agent_d2.language_lint import detect_mixed_language
        # 30+ chinese chars + lots of english content → over 15%.
        body = "中文段落开头" + "这是一段中文" * 5 + (
            " this is a long english passage that should trip the threshold "
            "because the ratio of ascii letters to cjk chars rises above 15 percent"
        )
        warn = detect_mixed_language(body, "zh-Hans")
        self.assertIsNotNone(warn)
        self.assertIn("language drift", warn)

    def test_language_lint_passes_clean_zh(self) -> None:
        from agentflow.agent_d2.language_lint import detect_mixed_language
        body = (
            "这是一段干净的中文内容。它讨论的是后端架构的演进路径，"
            "包括从单体到微服务，再到事件驱动架构的若干阶段。"
            "全程没有掺杂英文段落，仅在必要的术语处用括号注释一下。"
            "继续展开数据流的细节，从入口到出口的每一跳都应留下审计痕迹，"
            "便于事后追溯。下游消费者也需要约定明确的契约，避免漂移。"
        )
        warn = detect_mixed_language(body, "zh-Hans")
        self.assertIsNone(warn)

    def test_language_lint_whitelists_brand_terms(self) -> None:
        """Brand terms in the whitelist (AgentFlow, Telegram, Claude, API,
        Moonshot, Kimi, Anthropic, OpenAI, Jina, Atlas, Kafka, Redis,
        PostgreSQL, K8s) shouldn't trip the lint when the surrounding
        body is clearly Chinese."""
        from agentflow.agent_d2.language_lint import detect_mixed_language
        body = (
            "AgentFlow 的 Telegram 机器人和 Claude API 已经接通，"
            "整体走 Moonshot 兼容协议。Jina 用来生成嵌入，"
            "Atlas 处理图像，"
            "底层依赖 Kafka 加 Redis 加 PostgreSQL 这套常规组合，"
            "不打算引入 K8s 这种重型基础设施。"
            "目前所有任务都跑在单台机器上面观察效果，再扩展。"
            "如果未来要上集群，再切换到合适的编排层。"
            "下一步重点放在内容质量上，把约束做精细。"
        )
        warn = detect_mixed_language(body, "zh-Hans")
        self.assertIsNone(warn)


class DetectNextStepModeAwarenessTests(AgentflowHomeTestCase):
    """v1.0.14 — `_detect_next_step` used to block tg_review-mode operators
    on the Claude Code / Cursor skill-harness check, which is irrelevant
    when interaction happens entirely via Telegram. /start auto-dispatch
    surfaced this as 'still need to manually deploy service' even though
    the actual blocking step was the unrelated profile init."""

    def test_tg_review_mode_skips_skill_check(self) -> None:
        from agentflow.cli.bootstrap_commands import _detect_next_step

        env_path = self.home / ".env"
        env_path.write_text(
            "TELEGRAM_BOT_TOKEN=fake-tg-token\nMOCK_LLM=false\n",
            encoding="utf-8",
        )

        # No ~/.claude/skills, no ~/.cursor/skills, no topic_profiles.yaml.
        with patch("agentflow.cli.bootstrap_commands.Path") as path_mock:
            from pathlib import Path as _RealPath
            def _resolve(p):
                p_str = str(p)
                # Force the skill paths to "exist=False, iterdir=empty".
                if "/skills" in p_str:
                    fake = _RealPath(self.home / "nonexistent_skills_dir")
                    return fake
                return _RealPath(p_str)
            path_mock.side_effect = lambda p: _resolve(p)

            result = _detect_next_step(env_path)

        self.assertNotEqual(result["current_state"], "skills_not_installed")
        self.assertEqual(result["mode"], "tg_review")

    def test_tg_review_mode_reaches_profile_check_when_no_profile(self) -> None:
        """Without skill check blocking, tg_review mode should advance to
        the profile check and report missing_profile when topic_profiles.yaml
        does not exist — which is what /start auto-dispatch consumes to
        kick off the onboard wizard."""
        from agentflow.cli.bootstrap_commands import _detect_next_step

        env_path = self.home / ".env"
        env_path.write_text(
            "TELEGRAM_BOT_TOKEN=fake-tg-token\nMOCK_LLM=false\n",
            encoding="utf-8",
        )

        # Ensure topic_profiles.yaml does NOT exist in the test home, and
        # neither do skills. Patch the detector's home-relative reads.
        with patch(
            "agentflow.cli.bootstrap_commands.os.path.expanduser",
            side_effect=lambda p: str(self.home / p.lstrip("~/")),
        ):
            result = _detect_next_step(env_path)

        # Either missing_profile (preferred) or downstream check; the key
        # assertion is that we did NOT block on skills_not_installed.
        self.assertNotEqual(result["current_state"], "skills_not_installed")
        self.assertEqual(result["mode"], "tg_review")


class MarkdownV2EscapeRegressionTests(AgentflowHomeTestCase):
    """v1.0.13 — Telegram MarkdownV2 send_message used to fail with
    ``Bad Request: can't parse entities`` whenever a daemon-emitted
    operator message contained literal ``(`` / ``)`` / ``=`` that hadn't
    been backslash-escaped. Each test below exercises one of the affected
    code paths and asserts the body uses ``\\(`` / ``\\)`` / ``\\=`` in
    the literal positions where the bug was."""

    def _capture_messages(self, fn, *args, **kwargs) -> list[str]:
        sent: list[str] = []

        def _capture(_chat_id, text, *_a, **_kw):
            sent.append(text)

        with patch(
            "agentflow.agent_review.daemon.tg_client.send_message",
            side_effect=_capture,
        ):
            fn(*args, **kwargs)
        return sent

    def _seed_pending_article(self, article_id: str = "hs_test_001") -> None:
        from agentflow.agent_review import state
        from agentflow.shared.bootstrap import agentflow_home
        draft = agentflow_home() / "drafts" / article_id
        draft.mkdir(parents=True, exist_ok=True)
        (draft / "metadata.json").write_text(
            json.dumps({"title": "real title"}), encoding="utf-8",
        )
        state.transition(
            article_id, gate="A",
            to_state=state.STATE_DRAFT_PENDING_REVIEW,
            actor="test", decision="seed", force=True,
        )

    def test_status_summary_escapes_parens(self) -> None:
        self._seed_pending_article()
        sent = self._capture_messages(review_daemon._send_status_summary, 456)
        self.assertEqual(len(sent), 1)
        body = sent[0]
        self.assertIn("📊 *Pending* \\(", body)
        self.assertIn("\\)", body)
        self.assertNotIn("📊 *Pending* (", body)

    def test_queue_summary_escapes_parens(self) -> None:
        self._seed_pending_article()
        sent = self._capture_messages(review_daemon._send_queue_summary, 456)
        self.assertEqual(len(sent), 1)
        body = sent[0]
        self.assertIn("📋 *Queue* \\(top ", body)
        self.assertIn(" oldest\\)", body)
        self.assertNotIn("📋 *Queue* (", body)

    def test_auth_debug_escapes_parens(self) -> None:
        with patch(
            "agentflow.agent_review.daemon.auth.is_authorized", return_value=True,
        ):
            sent = self._capture_messages(
                review_daemon._send_auth_debug, 456, 123,
            )
        self.assertEqual(len(sent), 1)
        body = sent[0]
        self.assertIn("🔐 *Auth Debug* \\(uid `", body)
        self.assertIn("`\\)", body)
        self.assertNotIn("🔐 *Auth Debug* (uid", body)


class HotspotsMockLeakDoctorTests(AgentflowHomeTestCase):
    """v1.0.11 — `af doctor` surfaces historical mock-tagged hotspot files
    so the operator can `rm` them at their discretion."""

    def test_clean_dir_passes(self) -> None:
        from agentflow.agent_review import preflight
        (self.home / "hotspots").mkdir(parents=True, exist_ok=True)
        (self.home / "hotspots" / "2026-05-01.json").write_text(
            json.dumps({"hotspots": [{"id": "real_1", "topic_one_liner": "ok"}]}),
            encoding="utf-8",
        )
        result = preflight.check_hotspots_mock_leak()
        self.assertTrue(result.ok)
        self.assertIn("clean", result.message)

    def test_template_fingerprint_caught(self) -> None:
        from agentflow.agent_review import preflight
        (self.home / "hotspots").mkdir(parents=True, exist_ok=True)
        (self.home / "hotspots" / "2026-04-29.json").write_text(
            json.dumps({
                "hotspots": [{
                    "source_references": [{
                        "text_snippet": "Spent the day wiring Claude Code subagents into our QA loop"
                    }]
                }]
            }),
            encoding="utf-8",
        )
        result = preflight.check_hotspots_mock_leak()
        self.assertFalse(result.ok)
        self.assertIn("2026-04-29.json", result.message)
        self.assertIn("2026-04-29.json", result.extra["contaminated_files"])

    def test_raw_metadata_mock_true_caught(self) -> None:
        from agentflow.agent_review import preflight
        (self.home / "hotspots").mkdir(parents=True, exist_ok=True)
        (self.home / "hotspots" / "2026-04-23.json").write_text(
            json.dumps({"hotspots": [{"raw_metadata": {"mock": True}}]}),
            encoding="utf-8",
        )
        result = preflight.check_hotspots_mock_leak()
        self.assertFalse(result.ok)
        self.assertIn("2026-04-23.json", result.extra["contaminated_files"])

    def test_no_dir_passes(self) -> None:
        from agentflow.agent_review import preflight
        result = preflight.check_hotspots_mock_leak()
        self.assertTrue(result.ok)


class ViewpointMinerRealModeFailureTests(AgentflowHomeTestCase):
    """v1.0.12 — when LLM fails in real mode, viewpoint_miner.mine() must
    raise so run_d1_scan can drop the cluster instead of emitting a stub
    Hotspot with empty angles that looks real but isn't."""

    def _cluster(self, cluster_id: str = "c1") -> object:
        from agentflow.shared.models import RawSignal, TopicCluster
        return TopicCluster(
            cluster_id=cluster_id,
            signals=[RawSignal(
                source="hackernews",
                source_item_id="hn_1",
                author="hn",
                text="some real source text",
                url="https://example.com/x",
                published_at=datetime.now(timezone.utc),
                engagement={"score": 50},
            )],
            centroid_embedding=[0.1, 0.2],
            summary_one_liner="topic",
        )

    def test_mine_raises_in_real_mode_on_llm_failure(self) -> None:
        from agentflow.agent_d1 import viewpoint_miner
        from agentflow.shared.llm_client import LLMClient

        async def _boom(*_a, **_kw):
            raise RuntimeError("upstream LLM 500")

        with (
            patch.dict(os.environ, {"MOCK_LLM": "false"}, clear=False),
            patch.object(LLMClient, "chat_json", side_effect=_boom),
        ):
            with self.assertRaises(RuntimeError):
                asyncio.run(viewpoint_miner.mine(self._cluster(), {}, {}))

    def test_mine_returns_stub_in_mock_mode_on_fixture_miss(self) -> None:
        from agentflow.agent_d1 import viewpoint_miner
        from agentflow.shared.llm_client import LLMClient

        async def _boom(*_a, **_kw):
            raise FileNotFoundError("fixture missing")

        with (
            patch.dict(os.environ, {"MOCK_LLM": "true"}, clear=False),
            patch.object(LLMClient, "chat_json", side_effect=_boom),
        ):
            result = asyncio.run(viewpoint_miner.mine(self._cluster(), {}, {}))
        self.assertIsNotNone(result)
        self.assertEqual(result.recommended_series, "B")

    def test_run_d1_scan_drops_failed_clusters_in_real_mode(self) -> None:
        from agentflow.agent_d1 import main as d1_main
        from agentflow.shared.models import (
            RawSignal,
            TopicCluster,
        )

        good_cluster = self._cluster("good")
        bad_cluster = self._cluster("bad")

        async def _fake_collect(*_a, **_kw):
            return [good_cluster.signals[0], bad_cluster.signals[0]]

        async def _fake_cluster(_signals):
            return [good_cluster, bad_cluster]

        async def _fake_mine(cluster, *_, **__):
            if cluster.cluster_id == "bad":
                raise RuntimeError("LLM 500 for bad cluster")
            from agentflow.shared.models import Hotspot
            return Hotspot(
                id="hs_good",
                topic_one_liner="good topic",
                source_references=[],
                mainstream_views=[],
                overlooked_angles=[],
                recommended_series="B",
                series_confidence=0.5,
                suggested_angles=[],
                freshness_score=0.5,
                depth_potential="medium",
                generated_at=datetime.now(timezone.utc),
            )

        with (
            patch.dict(os.environ, {"MOCK_LLM": "false"}, clear=False),
            patch("agentflow.agent_d1.main._collect_all", side_effect=_fake_collect),
            patch("agentflow.agent_d1.main.clustering.cluster", side_effect=_fake_cluster),
            patch("agentflow.agent_d1.main.scoring.select_top", return_value=[good_cluster, bad_cluster]),
            patch("agentflow.agent_d1.main.viewpoint_miner.mine", side_effect=_fake_mine),
            patch("agentflow.agent_d1.main.viewpoint_miner.reset_id_counter"),
            patch("agentflow.agent_d1.main.load_style_profile", return_value={}),
            patch("agentflow.agent_d1.main.load_sources", return_value={}),
            patch("agentflow.agent_d1.main._load_content_matrix", return_value={}),
        ):
            output = asyncio.run(d1_main.run_d1_scan())

        self.assertEqual(len(output.hotspots), 1)
        self.assertEqual(output.hotspots[0].id, "hs_good")


class HotspotsMockGuardTests(AgentflowHomeTestCase):
    """v1.0.10 — refuse to let mock-tagged signals reach D1 output when
    MOCK_LLM is not explicitly opted into. Belt-and-suspenders against
    collector regressions, env misconfiguration, fixture seed leaks."""

    def _signal(
        self, source: str, item_id: str, mock: bool,
    ) -> object:
        from agentflow.shared.models import RawSignal
        return RawSignal(
            source=source,
            source_item_id=item_id,
            author=f"@{source}_user",
            text=f"text from {item_id}",
            url=f"https://example.com/{source}/{item_id}",
            published_at=datetime.now(timezone.utc),
            engagement={},
            raw_metadata={"mock": True} if mock else {"mock": False},
        )

    def test_real_mode_filters_mock_tagged_signals(self) -> None:
        from agentflow.agent_d1 import main as d1_main

        signals = [
            self._signal("twitter", "real_1", mock=False),
            self._signal("twitter", "mock_1", mock=True),
            self._signal("hackernews", "real_2", mock=False),
            self._signal("rss", "mock_2", mock=True),
        ]

        async def _fake_twitter(*_a, **_kw): return [signals[0], signals[1]]
        async def _fake_rss(*_a, **_kw): return [signals[3]]
        async def _fake_hn(*_a, **_kw): return [signals[2]]

        with (
            patch.dict(os.environ, {"MOCK_LLM": "false"}, clear=False),
            patch(
                "agentflow.agent_d1.main.twitter_collector.collect",
                side_effect=_fake_twitter,
            ),
            patch(
                "agentflow.agent_d1.main.rss_collector.collect",
                side_effect=_fake_rss,
            ),
            patch(
                "agentflow.agent_d1.main.hn_collector.collect",
                side_effect=_fake_hn,
            ),
        ):
            collected = asyncio.run(
                d1_main._collect_all(
                    {
                        "twitter_kols": [{"handle": "x"}],
                        "rss_feeds": [{"url": "https://example.com/feed"}],
                        "hackernews": {"enabled": True},
                    }
                )
            )

        self.assertEqual(len(collected), 2)
        ids = sorted(s.source_item_id for s in collected)
        self.assertEqual(ids, ["real_1", "real_2"])
        for sig in collected:
            meta = getattr(sig, "raw_metadata", None)
            self.assertFalse(
                isinstance(meta, dict) and meta.get("mock") is True,
                f"mock-tagged signal {sig.source_item_id} leaked into output",
            )

    def test_mock_mode_preserves_mock_signals(self) -> None:
        from agentflow.agent_d1 import main as d1_main

        signals = [
            self._signal("twitter", "mock_1", mock=True),
            self._signal("hackernews", "mock_2", mock=True),
        ]

        async def _fake_twitter(*_a, **_kw): return [signals[0]]
        async def _fake_rss(*_a, **_kw): return []
        async def _fake_hn(*_a, **_kw): return [signals[1]]

        with (
            patch.dict(os.environ, {"MOCK_LLM": "true"}, clear=False),
            patch(
                "agentflow.agent_d1.main.twitter_collector.collect",
                side_effect=_fake_twitter,
            ),
            patch(
                "agentflow.agent_d1.main.rss_collector.collect",
                side_effect=_fake_rss,
            ),
            patch(
                "agentflow.agent_d1.main.hn_collector.collect",
                side_effect=_fake_hn,
            ),
        ):
            collected = asyncio.run(
                d1_main._collect_all(
                    {
                        "twitter_kols": [{"handle": "x"}],
                        "rss_feeds": [],
                        "hackernews": {"enabled": True},
                    }
                )
            )

        self.assertEqual(len(collected), 2)


if __name__ == "__main__":
    unittest.main()
