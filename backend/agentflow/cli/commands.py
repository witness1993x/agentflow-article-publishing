"""`af` CLI entry point.

Every subcommand:

* Supports a ``--json`` flag where sensible so skills / scripts can parse stdout.
* Calls ``append_memory_event(event_type, ...)`` on mutations (mirrors the
  event types used by the legacy FastAPI routes: ``article_created``,
  ``fill_choices``, ``section_edit``, ``preview``, ``publish``,
  ``image_resolved``).
* Uses ``asyncio.run()`` to drive async agents.
* Raises ``click.ClickException`` for expected failures so callers can parse
  exit code + stderr.
"""

from __future__ import annotations

import asyncio
import json as _json
import os
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path
from typing import Any
from uuid import uuid4

import click


def _af_version() -> str:
    try:
        return _pkg_version("agentflow")
    except PackageNotFoundError:
        return "unknown"


def _load_dotenv_once() -> None:
    """Load ``backend/.env`` into ``os.environ`` without overriding existing vars.

    Walks up from this file to find ``backend/.env`` so the CLI works regardless
    of cwd. Silent if ``python-dotenv`` isn't installed or no ``.env`` exists.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return

    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / ".env"
        if candidate.is_file() and parent.name == "backend":
            load_dotenv(candidate, override=False)
            return
        if (parent / "pyproject.toml").is_file():
            candidate = parent / ".env"
            if candidate.is_file():
                load_dotenv(candidate, override=False)
            return


_load_dotenv_once()


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


def _make_article_id(hotspot_id: str) -> str:
    return f"{hotspot_id}-{_stamp()}-{uuid4().hex[:8]}"


def _hotspots_dir() -> Path:
    from agentflow.shared.hotspot_store import hotspots_dir

    return hotspots_dir()


def _search_results_dir() -> Path:
    from agentflow.shared.hotspot_store import search_results_dir

    return search_results_dir()


def _drafts_dir() -> Path:
    from agentflow.shared.bootstrap import agentflow_home

    return agentflow_home() / "drafts"


def _draft_dir(article_id: str) -> Path:
    return _drafts_dir() / article_id


def _save_search_output(
    output: Any,
    *,
    slug: str,
    stamp: str | None = None,
    extra: dict[str, Any] | None = None,
) -> Path:
    path = _search_results_dir()
    path.mkdir(parents=True, exist_ok=True)
    stamp = stamp or datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    out_path = path / f"search_{slug}_{stamp}.json"
    payload = {
        **(output.to_dict() if hasattr(output, "to_dict") else dict(output)),
        "kind": "search_result",
        **(extra or {}),
    }
    out_path.write_text(
        _json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return out_path


def _iter_hotspot_files(limit_days: int | None = 7) -> list[Path]:
    """Return lookup files for hotspot resolution."""
    from agentflow.shared.hotspot_store import iter_lookup_files

    return iter_lookup_files(limit_days=limit_days, include_search_results=True)


def _find_hotspot(hotspot_id: str, date: str | None = None) -> dict[str, Any]:
    """Scan hotspot files for a given id. If ``date`` is given, only that file.

    Returns the raw hotspot dict (not a Hotspot dataclass).
    """
    from agentflow.shared.hotspot_store import find_hotspot_record

    files = [_hotspots_dir() / f"{date}.json"] if date else _iter_hotspot_files(limit_days=7)
    try:
        hotspot, _ = find_hotspot_record(
            hotspot_id,
            date=date,
            limit_days=7,
            include_search_results=True,
        )
        return hotspot
    except KeyError:
        pass
    raise click.ClickException(
        f"Hotspot {hotspot_id!r} not found (searched "
        f"{len(files)} file(s) under ~/.agentflow/hotspots and ~/.agentflow/search_results/)."
    )


def _emit_json(obj: Any) -> None:
    click.echo(_json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _load_style_profile_safe() -> dict[str, Any]:
    from agentflow.config.style_loader import load_style_profile

    try:
        return load_style_profile()
    except Exception:
        return {}


def _dedupe_keep_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _load_current_intent_safe() -> dict[str, Any] | None:
    from agentflow.shared.memory import load_current_intent

    try:
        return load_current_intent()
    except Exception:
        return None


def _active_profile_id(
    explicit_profile_id: str | None,
    active_intent: dict[str, Any] | None,
) -> str | None:
    if explicit_profile_id:
        return explicit_profile_id
    if not active_intent:
        return _default_topic_profile_id()
    profile = active_intent.get("profile") or {}
    text = str(profile.get("id") or "").strip()
    return text or _default_topic_profile_id()


def _default_topic_profile_id() -> str | None:
    text = str(os.environ.get("AGENTFLOW_DEFAULT_TOPIC_PROFILE") or "").strip()
    return text or None


def _maybe_trigger_profile_setup(profile_id: str | None, *, reason: str) -> dict[str, Any] | None:
    if not profile_id:
        return None
    try:
        from agentflow.agent_review import triggers as _triggers
        from agentflow.shared.topic_profile_lifecycle import (
            constraint_sessions_dir,
            save_session,
            user_profile_bootstrap_state,
        )
        from agentflow.shared.agent_bridge import emit_agent_event

        state = user_profile_bootstrap_state(profile_id)
        missing_fields = list(state.get("missing_fields") or [])
        if not missing_fields:
            return None
        for path in sorted(constraint_sessions_dir().glob("*.json"), reverse=True):
            try:
                session = _json.loads(path.read_text(encoding="utf-8")) or {}
            except Exception:
                continue
            if not isinstance(session, dict):
                continue
            if session.get("profile_id") != profile_id:
                continue
            if session.get("status") in {"pending", "collecting"}:
                return None
        session = {
            "profile_id": profile_id,
            "mode": "update" if state.get("user_profile_exists") else "init",
            "status": "pending",
            "source": reason,
            "reason": reason,
            "missing_fields": missing_fields,
            "answers": {},
            "created_at": _now_iso(),
        }
        path = save_session(session)
        mode = str(session.get("mode") or "")
        emit_agent_event(
            source="agentflow.cli",
            event_type="profile.setup_requested",
            payload={
                "profile_id": profile_id,
                "reason": reason,
                "missing_fields": missing_fields,
                "session_path": str(path),
                "mode": mode,
            },
        )
        return _triggers.post_profile_setup_prompt(
            profile_id=profile_id,
            reason=reason,
            missing_fields=missing_fields,
            session_path=str(path),
        )
    except Exception:
        return None


def _resolve_topic_profile(
    profile_id: str | None,
    *,
    allow_missing: bool = False,
) -> tuple[str | None, dict[str, Any] | None]:
    if not profile_id:
        return None, None
    from agentflow.shared.topic_profiles import (
        TopicProfileNotFoundError,
        get_topic_profile,
    )

    try:
        return profile_id, get_topic_profile(profile_id)
    except TopicProfileNotFoundError as err:
        if allow_missing:
            return profile_id, None
        raise click.ClickException(str(err)) from err


def _profile_meta(
    profile_id: str | None,
    profile: dict[str, Any] | None,
) -> dict[str, Any]:
    if not profile_id or not profile:
        return {}
    from agentflow.shared.topic_profiles import topic_profile_keywords_payload, topic_profile_label

    return {
        "profile_id": profile_id,
        "profile_label": topic_profile_label(profile, profile_id),
        "keywords": topic_profile_keywords_payload(profile),
    }


def _intent_to_filter_pattern(intent: dict[str, Any] | None) -> str | None:
    if not intent:
        return None
    from agentflow.shared.memory import intent_keyword_terms, intent_query_text

    keywords = intent_keyword_terms(intent)
    if keywords:
        import re

        escaped = [re.escape(term) for term in keywords if str(term).strip()]
        return "|".join(escaped) if escaped else None

    query_text = intent_query_text(intent)
    if not query_text:
        return None
    query = intent.get("query") or {}
    mode = str(query.get("mode") or "keyword")
    if mode == "regex":
        return query_text
    import re

    return re.escape(query_text)


def _combine_filter_patterns(*patterns: str | None) -> str | None:
    parts = _dedupe_keep_order([p for p in patterns if isinstance(p, str) and p.strip()])
    if not parts:
        return None
    return "|".join(f"(?:{part})" for part in parts)


def _filter_source_label(
    *,
    filter_pattern: str | None,
    topic_profile_id: str | None,
    active_intent: dict[str, Any] | None,
) -> str:
    if topic_profile_id:
        return "topic_profile"
    if active_intent and not filter_pattern:
        return "current_intent"
    return "cli_flag"


def _filter_boundary_summary(*, matched: int, total: int) -> dict[str, Any]:
    if total <= 0:
        return {
            "level": "empty",
            "ratio": 0.0,
            "recommendation": "scan returned no hotspots; check sources or widen inputs",
        }
    ratio = matched / total
    if matched == 0:
        return {
            "level": "too_narrow",
            "ratio": ratio,
            "recommendation": "0 matched; broaden the regex or remove some terms",
        }
    if ratio <= 0.2:
        return {
            "level": "narrow",
            "ratio": ratio,
            "recommendation": "very selective filter; inspect filtered_out_preview before deciding",
        }
    if ratio >= 0.8:
        return {
            "level": "broad",
            "ratio": ratio,
            "recommendation": "filter is broad; consider tightening if the topic feels noisy",
        }
    return {
        "level": "balanced",
        "ratio": ratio,
        "recommendation": "match set looks focused enough for direct review",
    }


def _hotspot_filter_preview(hotspots: list[Any], *, limit: int = 5) -> list[dict[str, Any]]:
    preview: list[dict[str, Any]] = []
    for hotspot in hotspots[:limit]:
        data = hotspot.to_dict() if hasattr(hotspot, "to_dict") else dict(hotspot)
        preview.append(
            {
                "id": data.get("id"),
                "topic_one_liner": data.get("topic_one_liner"),
                "recommended_series": data.get("recommended_series"),
                "freshness_score": data.get("freshness_score"),
            }
        )
    return preview


def _hotspot_haystack(hotspot: Any) -> str:
    data = hotspot.to_dict() if hasattr(hotspot, "to_dict") else hotspot
    parts: list[str] = []
    parts.append(str(data.get("topic_one_liner", "")))
    for angle in data.get("suggested_angles") or []:
        if isinstance(angle, dict):
            parts.append(str(angle.get("title", "")))
            parts.append(str(angle.get("angle", "")))
            parts.append(str(angle.get("hook", "")))
    for ref in data.get("source_references") or []:
        if isinstance(ref, dict):
            parts.append(str(ref.get("text_snippet", "")))
            parts.append(str(ref.get("title", "")))
    return "\n".join(parts)


def _merge_source_references(existing: Any, incoming: Any) -> None:
    existing_refs = list(getattr(existing, "source_references", []) or [])
    seen = {
        _json.dumps(ref, ensure_ascii=False, sort_keys=True, default=str)
        for ref in existing_refs
        if isinstance(ref, dict)
    }
    for ref in getattr(incoming, "source_references", []) or []:
        if not isinstance(ref, dict):
            continue
        key = _json.dumps(ref, ensure_ascii=False, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        existing_refs.append(ref)
    if existing_refs:
        existing.source_references = existing_refs


def _merge_hotspots_keep_best(scan_hotspots: list[Any], search_hotspots: list[Any]) -> list[Any]:
    merged: list[Any] = []
    by_id: dict[str, Any] = {}
    by_topic: dict[str, Any] = {}

    def _add(hotspot: Any) -> None:
        data = hotspot.to_dict() if hasattr(hotspot, "to_dict") else dict(hotspot)
        hs_id = str(data.get("id") or "").strip()
        topic = str(data.get("topic_one_liner") or "").strip().casefold()
        existing = by_id.get(hs_id) if hs_id else None
        if existing is None and topic:
            existing = by_topic.get(topic)
        if existing is not None:
            _merge_source_references(existing, hotspot)
            return
        merged.append(hotspot)
        if hs_id:
            by_id[hs_id] = hotspot
        if topic:
            by_topic[topic] = hotspot

    for item in scan_hotspots:
        _add(item)
    for item in search_hotspots:
        _add(item)
    return merged


def _profile_rerank_hotspots(
    hotspots: list[Any],
    *,
    profile: dict[str, Any],
    profile_id: str,
    filter_pattern: str | None,
    target_candidates: int,
) -> tuple[list[Any], dict[str, Any], dict[str, Any] | None]:
    import re

    from agentflow.agent_d1.topic_fit import score_profile_fit

    compiled = None
    filter_meta: dict[str, Any] | None = None
    if filter_pattern:
        try:
            compiled = re.compile(filter_pattern, re.IGNORECASE)
        except re.error as err:
            raise click.UsageError(f"invalid effective filter regex: {err}")

    scored: list[tuple[Any, float, float, bool]] = []
    matched: list[Any] = []
    filtered_out: list[Any] = []
    for hotspot in hotspots:
        data = hotspot.to_dict() if hasattr(hotspot, "to_dict") else dict(hotspot)
        fit = float(score_profile_fit(data, profile, profile_id=profile_id))
        freshness = float(data.get("freshness_score") or 0.0)
        regex_hit = bool(compiled.search(_hotspot_haystack(hotspot))) if compiled else False
        if compiled:
            (matched if regex_hit else filtered_out).append(hotspot)
        # Regex is now a ranking hint in profile mode, not a hard gate.
        composite = (0.70 * fit) + (0.25 * freshness) + (0.05 if regex_hit else 0.0)
        scored.append((hotspot, composite, fit, regex_hit))

    scored.sort(key=lambda item: item[1], reverse=True)
    limit = max(1, int(target_candidates or len(scored))) if scored else 0
    kept = [item[0] for item in scored[:limit]]
    fit_by_id = {id(item[0]): (item[1], item[2], item[3]) for item in scored}
    preview = []
    for hotspot in kept[:5]:
        data = hotspot.to_dict() if hasattr(hotspot, "to_dict") else dict(hotspot)
        composite, fit, regex_hit = fit_by_id.get(id(hotspot), (0.0, 0.0, False))
        preview.append(
            {
                "id": data.get("id"),
                "topic_one_liner": data.get("topic_one_liner"),
                "topic_fit_score": round(float(fit), 4),
                "rerank_score": round(float(composite), 4),
                "regex_match": bool(regex_hit),
            }
        )

    rerank_meta = {
        "strategy": "topic_fit_freshness_regex_hint",
        "kept_count": len(kept),
        "topic_fit_preview": preview,
    }
    if compiled:
        filter_meta = {
            "pattern": filter_pattern,
            "source": "topic_profile",
            "matched": len(matched),
            "total": len(hotspots),
            "filtered_out": len(filtered_out),
            "filtered_out_preview": _hotspot_filter_preview(filtered_out),
            "boundary": _filter_boundary_summary(
                matched=len(matched),
                total=len(hotspots),
            ),
            "mode": "soft_rerank",
        }
    return kept, rerank_meta, filter_meta


def _merge_profile_search_outputs(
    outputs: list[tuple[str, Any, Path]],
    *,
    target_candidates: int,
) -> Any:
    from agentflow.shared.models import D1Output

    if not outputs:
        return D1Output(generated_at=datetime.now(timezone.utc), hotspots=[])

    hotspots = []
    seen_topics: set[str] = set()
    generated_at = outputs[0][1].generated_at
    for _, output, _ in outputs:
        if output.generated_at and output.generated_at > generated_at:
            generated_at = output.generated_at
        for hotspot in output.hotspots:
            key = str(getattr(hotspot, "topic_one_liner", "") or "").strip().casefold()
            if key and key in seen_topics:
                continue
            if key:
                seen_topics.add(key)
            hotspots.append(hotspot)
            if len(hotspots) >= target_candidates:
                return D1Output(generated_at=generated_at, hotspots=hotspots)
    return D1Output(generated_at=generated_at, hotspots=hotspots)


# ---------------------------------------------------------------------------
# CLI root
# ---------------------------------------------------------------------------


@click.group(help="AgentFlow Article Publishing CLI")
@click.version_option(_af_version(), prog_name="af")
def cli() -> None:
    pass


# ---------------------------------------------------------------------------
# af learn-style
# ---------------------------------------------------------------------------


@cli.command(
    "learn-style",
    help="Learn (or re-learn) the style profile from sample articles.",
)
@click.option(
    "--dir",
    "dir_",
    type=click.Path(exists=True, file_okay=False, dir_okay=True),
    default=None,
    help="Directory of sample articles to ingest.",
)
@click.option(
    "--file",
    "file_",
    type=click.Path(exists=True, file_okay=True, dir_okay=False),
    multiple=True,
    help="Single sample article (md/docx/txt). Repeatable.",
)
@click.option(
    "--url",
    multiple=True,
    help="URL of a published article to ingest. Repeatable.",
)
@click.option(
    "--from-published",
    is_flag=True,
    default=False,
    help="Re-ingest all drafts marked as published.",
)
@click.option(
    "--show",
    is_flag=True,
    default=False,
    help="Print the current style profile and source list.",
)
@click.option(
    "--recompute",
    is_flag=True,
    default=False,
    help="Re-aggregate the style profile from the full corpus.",
)
def learn_style(
    dir_: str | None,
    file_: tuple[str, ...],
    url: tuple[str, ...],
    from_published: bool,
    show: bool,
    recompute: bool,
) -> None:
    """Dispatch to Agent D0."""
    has_sources = bool(dir_ or file_ or url)
    if not (has_sources or show or recompute or from_published):
        raise click.UsageError(
            "Provide at least one of --dir / --file / --url, or use "
            "--show / --recompute / --from-published."
        )

    try:
        from agentflow.agent_d0.main import run as _run  # type: ignore
    except Exception as err:  # pragma: no cover
        click.echo(f"learn-style: failed to load Agent D0: {err}", err=True)
        raise click.Abort()

    try:
        _run(
            dir_=dir_,
            file_=list(file_) if file_ else None,
            url=list(url) if url else None,
            from_published=from_published,
            show=show,
            recompute=recompute,
        )
    except ValueError as err:
        raise click.UsageError(str(err))
    except Exception as err:  # surface unexpected failures
        click.echo(f"learn-style failed: {err}", err=True)
        raise


# ---------------------------------------------------------------------------
# af learn-from-handle — one-shot RSS-driven seeding
# ---------------------------------------------------------------------------


@cli.command(
    "learn-from-handle",
    help="Seed the style corpus from a Medium / Substack / Mirror / RSS handle. "
    "Fetches the latest N posts and pipes them through `af learn-style --url`. "
    "With --profile <id>, also writes a per-profile style_signature into "
    "topic_profiles.yaml and emits keyword candidates as suggestions.",
)
@click.argument("handle_or_url")
@click.option(
    "--max-samples",
    type=int,
    default=5,
    show_default=True,
    help="How many recent posts to ingest from the feed (1–20).",
)
@click.option(
    "--ask-extras/--no-ask-extras",
    default=True,
    show_default=True,
    help="Interactively prompt for an optional description + extra sample after ingestion.",
)
@click.option(
    "--profile",
    "profile_id",
    type=str,
    default=None,
    help="Topic profile id to receive a per-profile style_signature slice + "
    "keyword candidate suggestions. Without this flag the legacy global "
    "~/.agentflow/style_profile.yaml is the only sink.",
)
@click.option(
    "--max-keyword-candidates",
    "max_keyword_candidates",
    type=int,
    default=20,
    show_default=True,
    help="Cap on keyword candidates emitted as suggestions (only when --profile is set).",
)
@click.option("--json", "as_json", is_flag=True, default=False)
def learn_from_handle(
    handle_or_url: str,
    max_samples: int,
    ask_extras: bool,
    profile_id: str | None,
    max_keyword_candidates: int,
    as_json: bool,
) -> None:
    from agentflow.agent_d0.handle_fetcher import resolve_handle_to_urls
    from agentflow.agent_d0.main import run as _run
    from agentflow.shared.bootstrap import agentflow_home as _home

    if max_samples < 1 or max_samples > 20:
        raise click.UsageError("--max-samples must be between 1 and 20")
    if max_keyword_candidates < 0:
        raise click.UsageError("--max-keyword-candidates must be >= 0")

    try:
        urls, label = resolve_handle_to_urls(handle_or_url, max_samples=max_samples)
    except ValueError as err:
        raise click.UsageError(str(err))
    except Exception as err:  # pragma: no cover
        raise click.ClickException(f"could not resolve {handle_or_url!r}: {err}")

    if not urls:
        raise click.ClickException(
            f"no article URLs found for {handle_or_url!r} ({label}). "
            "If this is a custom blog, pass the RSS feed URL directly."
        )

    click.echo(f"resolved: {label}")
    click.echo(f"top {len(urls)} article URL(s):")
    for u in urls:
        click.echo(f"  - {u}")
    click.echo()

    # Ingest via existing D0 pipeline. Each URL becomes one corpus entry +
    # contributes to the aggregated (global) style profile. We always run this
    # so the legacy ~/.agentflow/style_profile.yaml stays in sync.
    try:
        result = _run(url=urls)
    except Exception as err:
        raise click.ClickException(f"D0 ingestion failed: {err}")

    summary: dict[str, Any] = {
        "handle_or_url": handle_or_url,
        "resolved_label": label,
        "urls_ingested": urls,
        "corpus_entries_added": (result or {}).get("articles_added")
        if isinstance(result, dict)
        else None,
        "extras": None,
        "profile_id": profile_id,
        "style_signature_written": False,
        "keyword_candidate_suggestions": [],
    }

    # ---- per-profile slice (style_signature + keyword suggestions) -------
    if profile_id:
        try:
            profile_outcome = _learn_from_handle_per_profile(
                profile_id=profile_id,
                urls=urls,
                label=label,
                global_profile=result if isinstance(result, dict) else {},
                max_keyword_candidates=max_keyword_candidates,
            )
        except Exception as err:
            click.echo(
                f"  → per-profile slice failed (global profile still saved): {err}",
                err=True,
            )
            profile_outcome = {
                "style_signature_written": False,
                "keyword_candidate_suggestions": [],
                "error": str(err),
            }

        summary["style_signature_written"] = profile_outcome.get(
            "style_signature_written", False
        )
        summary["keyword_candidate_suggestions"] = profile_outcome.get(
            "keyword_candidate_suggestions", []
        )
        if profile_outcome.get("style_signature_path"):
            summary["style_signature_path"] = profile_outcome["style_signature_path"]
        if profile_outcome.get("error"):
            summary["per_profile_error"] = profile_outcome["error"]

    if ask_extras and not as_json:
        click.echo()
        desc = click.prompt(
            "Optional: a one-line description of your voice (Enter to skip)",
            default="",
            show_default=False,
        ).strip()
        if desc:
            desc_path = _home() / "style_corpus" / "manual_description.md"
            desc_path.parent.mkdir(parents=True, exist_ok=True)
            existing = desc_path.read_text(encoding="utf-8") if desc_path.exists() else ""
            new_block = f"\n## from learn-from-handle ({label})\n\n{desc}\n"
            desc_path.write_text(existing + new_block, encoding="utf-8")
            click.echo(f"  → saved description to {desc_path}")
            summary["extras"] = {"description_path": str(desc_path)}
        else:
            click.echo("  (no description added)")

        more = click.prompt(
            "Paste an extra sample article URL? (Enter to skip)",
            default="",
            show_default=False,
        ).strip()
        if more:
            try:
                _run(url=[more])
                click.echo(f"  → ingested extra sample: {more}")
                summary.setdefault("extras", {})["extra_url"] = more
            except Exception as err:
                click.echo(f"  → extra sample failed: {err}", err=True)

    if as_json:
        _emit_json(summary)
        return
    click.echo()
    click.echo(f"done. style profile saved at ~/.agentflow/style_profile.yaml")
    if profile_id:
        if summary["style_signature_written"]:
            click.echo(
                f"  → wrote style_signature to topic_profiles.yaml::profiles.{profile_id}"
                f".publisher_account.style_signature"
            )
        n_sugg = len(summary["keyword_candidate_suggestions"])
        if n_sugg:
            click.echo(
                f"  → emitted {n_sugg} keyword-candidate suggestion(s); "
                "review with `af topic-profile suggestion-list --profile "
                f"{profile_id}`"
            )
        elif max_keyword_candidates > 0:
            click.echo("  → no keyword candidates surfaced from this batch")
    click.echo("next: `af learn-style --show` to inspect the aggregated profile")


def _learn_from_handle_per_profile(
    *,
    profile_id: str,
    urls: list[str],
    label: str,
    global_profile: dict[str, Any],
    max_keyword_candidates: int,
) -> dict[str, Any]:
    """Per-profile slice: write style_signature + emit keyword suggestions.

    Reuses the just-ingested URLs by re-extracting them locally (cheap, the
    extractor is the same one D0 just ran) so we can derive both per-article
    analyses for the keyword extractor and the slimmed style_signature for
    topic_profiles.yaml. Errors here never abort the global flow above.
    """
    from agentflow.agent_d0 import extractor, keyword_extractor
    from agentflow.shared.topic_profile_lifecycle import (
        make_learning_suggestion,
        upsert_profile,
    )

    outcome: dict[str, Any] = {
        "style_signature_written": False,
        "keyword_candidate_suggestions": [],
    }

    # ---- 1. Re-extract articles for this batch (independent of corpus state)
    articles = extractor.extract_sources(urls)
    if not articles:
        outcome["error"] = "no readable articles for per-profile slice"
        return outcome

    # ---- 2. Build a slimmed style_signature from the just-saved global profile
    signature = _derive_style_signature(global_profile, label=label, urls=urls)
    if signature:
        upsert_result = upsert_profile(
            profile_id,
            {"publisher_account": {"style_signature": signature}},
            replace_lists=False,
            source=f"learn_from_handle:profile={profile_id}",
        )
        outcome["style_signature_written"] = True
        outcome["style_signature_path"] = upsert_result.get("path")

    # ---- 3. Extract keyword candidates → emit one suggestion per candidate
    if max_keyword_candidates > 0:
        try:
            candidates = asyncio.run(
                keyword_extractor.extract_keyword_candidates(
                    articles, max_candidates=max_keyword_candidates
                )
            )
        except Exception as err:
            click.echo(
                f"  → keyword candidate extraction failed: {err}",
                err=True,
            )
            candidates = []

        for cand in candidates:
            group = cand.get("group") or "core"
            keyword = cand.get("keyword") or ""
            if not keyword.strip():
                continue
            evidence_text = cand.get("evidence") or ""
            patch = {
                "proposed_patch": {
                    "keyword_groups": {group: [keyword]},
                },
                "evidence": [
                    {
                        "kind": "learn_from_handle",
                        "handle_label": label,
                        "urls": list(urls),
                        "note": evidence_text,
                    }
                ],
            }
            try:
                suggestion = make_learning_suggestion(
                    profile_id=profile_id,
                    stage="keyword_groups",
                    title=f"keyword candidate: {keyword} ({group})",
                    summary=(
                        f"Auto-extracted from learn-from-handle ({label}). "
                        f"Group={group}. {evidence_text}"
                    ).strip(),
                    proposed_patch=patch["proposed_patch"],
                    evidence=patch["evidence"],
                    risk_level="low",
                )
                outcome["keyword_candidate_suggestions"].append(
                    {
                        "id": suggestion.get("id"),
                        "group": group,
                        "keyword": keyword,
                    }
                )
            except Exception as err:  # pragma: no cover
                click.echo(
                    f"  → could not save suggestion for {keyword!r}: {err}",
                    err=True,
                )

    return outcome


def _derive_style_signature(
    global_profile: dict[str, Any],
    *,
    label: str,
    urls: list[str],
) -> dict[str, Any]:
    """Project the freshly-aggregated global profile into a small per-profile
    signature. Conservative subset — never includes the full style_profile."""
    if not isinstance(global_profile, dict) or not global_profile:
        return {}

    def _pick(key: str) -> Any:
        val = global_profile.get(key)
        return val if val is not None else None

    signature: dict[str, Any] = {
        "source": "learn_from_handle",
        "handle_label": label,
        "sample_urls": list(urls),
        "learned_at": _now_iso(),
    }

    # Carry the small dict-shaped fields so downstream prompts can riff
    # without pulling in the global file.
    for key in ("voice_principles", "tone", "paragraph_preferences",
                "emoji_preferences", "citation_preferences", "taboos"):
        val = _pick(key)
        if val:
            signature[key] = val

    meta = global_profile.get("_meta")
    if isinstance(meta, dict):
        signature["_meta"] = {
            "source_article_count": meta.get("source_article_count"),
            "recompute_generation": meta.get("recompute_generation"),
            "generated_at": meta.get("generated_at"),
        }

    return signature


# ---------------------------------------------------------------------------
# af hotspots
# ---------------------------------------------------------------------------


@cli.command("hotspots", help="Run Agent D1 hotspot scan.")
@click.option(
    "--scan-window-hours",
    type=int,
    default=24,
    show_default=True,
    help="Only cluster signals newer than this many hours.",
)
@click.option(
    "--target-candidates",
    type=int,
    default=20,
    show_default=True,
    help="Target number of hotspots to emit (upper bound).",
)
@click.option(
    "--filter",
    "filter_pattern",
    type=str,
    default=None,
    help="Regex (case-insensitive) to keep only matching hotspots. Matched "
    "against topic_one_liner, suggested_angles titles, and source_reference "
    "text snippets. See docs/backlog/TOPIC_INTENT_FRAMEWORK.md.",
)
@click.option(
    "--profile",
    "topic_profile_id",
    type=str,
    default=None,
    help="Reusable topic profile id from ~/.agentflow/topic_profiles.yaml "
    "(falls back to config-examples/topic_profiles.example.yaml).",
)
@click.option(
    "--gate-a-top-k",
    "gate_a_top_k",
    type=int,
    default=3,
    show_default=True,
    help="How many candidates to surface in the Gate A TG card (1–10). "
    "Useful for tightening or loosening cron's daily review surface.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit the full D1Output dict as JSON on stdout.",
)
def hotspots(
    scan_window_hours: int,
    target_candidates: int,
    filter_pattern: str | None,
    topic_profile_id: str | None,
    gate_a_top_k: int,
    as_json: bool,
) -> None:
    """Run D1 synchronously. With ``--json``, print the full D1Output dict.

    With ``--filter``, post-filter the clusters by regex match on
    topic_one_liner, suggested angle titles, and source reference snippets.
    """
    from agentflow.agent_d1.main import run_d1_scan
    from agentflow.shared.memory import append_memory_event
    from agentflow.shared.topic_profile_learning import suggest_from_hotspots

    # Preflight: refuse to spend D1 budget if no LLM/embedding provider works.
    if (os.environ.get("MOCK_LLM") or "").lower() != "true":
        try:
            from agentflow.agent_review import preflight as _pf
            _pf.assert_ready_for_hotspots()
        except Exception as _err:
            raise click.ClickException(
                f"hotspots preflight failed: {_err}\n"
                "Run `af doctor` for details, or set MOCK_LLM=true."
            )

    active_intent = _load_current_intent_safe()
    active_profile_id = _active_profile_id(topic_profile_id, active_intent)
    resolved_profile_id, topic_profile = _resolve_topic_profile(
        active_profile_id,
        allow_missing=True,
    )
    profile_filter = None
    if topic_profile is not None:
        from agentflow.shared.topic_profiles import topic_profile_regex

        profile_filter = topic_profile_regex(topic_profile)
    effective_filter_pattern = _combine_filter_patterns(
        filter_pattern,
        profile_filter,
        None if (filter_pattern or profile_filter) else _intent_to_filter_pattern(active_intent),
    )

    output = asyncio.run(
        run_d1_scan(
            scan_window_hours=scan_window_hours,
            target_candidates=target_candidates,
        )
    )

    all_hotspots = list(output.hotspots)
    total_before = len(output.hotspots)
    filter_meta: dict[str, Any] | None = None
    recall_meta: dict[str, Any] | None = None
    rerank_meta: dict[str, Any] | None = None
    filtered_out: list[Any] = []
    config_suggestions: list[dict[str, Any]] = []

    if topic_profile is not None and resolved_profile_id:
        from agentflow.agent_d1.search import run_d1_search
        from agentflow.shared.topic_profiles import topic_profile_search_queries

        search_outputs = []
        search_hotspots: list[Any] = []
        search_queries = topic_profile_search_queries(topic_profile, resolved_profile_id)
        per_query_target = max(1, min(5, int(target_candidates or 5)))
        for query_text in search_queries:
            search_output, saved_path = asyncio.run(
                run_d1_search(
                    query=query_text,
                    days=7,
                    min_points=10,
                    target_candidates=per_query_target,
                )
            )
            search_outputs.append((query_text, search_output, saved_path))
            search_hotspots.extend(list(search_output.hotspots))

        merged_hotspots = _merge_hotspots_keep_best(all_hotspots, search_hotspots)
        kept, rerank_meta, filter_meta = _profile_rerank_hotspots(
            merged_hotspots,
            profile=topic_profile,
            profile_id=resolved_profile_id,
            filter_pattern=effective_filter_pattern,
            target_candidates=target_candidates,
        )
        output.hotspots = kept
        recall_meta = {
            "scan_count": len(all_hotspots),
            "search_count": len(search_hotspots),
            "merged_count": len(merged_hotspots),
            "kept_count": len(output.hotspots),
            "strategy": "scan_plus_profile_search_bundle",
            "queries": search_queries,
            "search_result_paths": [str(path) for _, _, path in search_outputs],
        }
        filter_source = _filter_source_label(
            filter_pattern=filter_pattern,
            topic_profile_id=resolved_profile_id,
            active_intent=active_intent,
        )
        append_memory_event(
            "topic_intent_used",
            article_id=None,
            payload={
                "command": "hotspots",
                "mode": "hybrid_recall",
                "query": effective_filter_pattern,
                "queries": search_queries,
                "source": filter_source,
                "matched_count": (filter_meta or {}).get("matched"),
                "total_count": (filter_meta or {}).get("total", len(merged_hotspots)),
                "scan_count": len(all_hotspots),
                "search_count": len(search_hotspots),
                "merged_count": len(merged_hotspots),
                **_profile_meta(resolved_profile_id, topic_profile),
            },
        )
    elif effective_filter_pattern:
        import re

        try:
            pattern = re.compile(effective_filter_pattern, re.IGNORECASE)
        except re.error as err:
            raise click.UsageError(f"invalid effective filter regex: {err}")

        kept = [h for h in all_hotspots if pattern.search(_hotspot_haystack(h))]
        filtered_out = [h for h in all_hotspots if not pattern.search(_hotspot_haystack(h))]
        output.hotspots = kept
        filter_source = _filter_source_label(
            filter_pattern=filter_pattern,
            topic_profile_id=topic_profile_id,
            active_intent=active_intent,
        )
        filter_meta = {
            "pattern": effective_filter_pattern,
            "source": filter_source,
            "matched": len(kept),
            "total": total_before,
            "filtered_out": len(filtered_out),
            "filtered_out_preview": _hotspot_filter_preview(filtered_out),
            "boundary": _filter_boundary_summary(
                matched=len(kept),
                total=total_before,
            ),
        }

        append_memory_event(
            "topic_intent_used",
            article_id=None,
            payload={
                "command": "hotspots",
                "mode": "regex",
                "query": effective_filter_pattern,
                "source": filter_source,
                "matched_count": len(kept),
                "total_count": total_before,
                **_profile_meta(topic_profile_id, topic_profile),
            },
        )

        if not kept:
            click.echo(
                f"filter {effective_filter_pattern!r} matched 0 of {total_before} hotspots. "
                "Consider broadening the regex.",
                err=True,
            )

    _maybe_trigger_profile_setup(active_profile_id, reason="hotspots")
    if active_profile_id and filter_meta:
        try:
            suggestion = suggest_from_hotspots(
                profile_id=active_profile_id,
                filter_meta=filter_meta,
            )
            if suggestion:
                config_suggestions.append(suggestion)
                append_memory_event(
                    "topic_profile_suggestion_created",
                    payload={
                        "profile_id": active_profile_id,
                        "suggestion_id": suggestion["id"],
                        "stage": "hotspots",
                    },
                )
        except Exception:
            pass

    # Auto-post Gate A topic-batch card to TG if there's at least one hotspot
    # and TG is configured. Failures must NOT break the hotspots command.
    if output.hotspots:
        try:
            from agentflow.agent_review import triggers as _triggers
            from agentflow.shared.bootstrap import agentflow_home as _home

            publisher_brand = "default"
            avoid: list[str] = []
            pub: dict[str, Any] = {}
            if topic_profile is not None:
                from agentflow.shared.topic_profiles import (
                    topic_profile_publisher_account,
                    topic_profile_avoid_terms,
                )
                pub = topic_profile_publisher_account(topic_profile)
                publisher_brand = str(pub.get("brand") or resolved_profile_id or "default")
                avoid = topic_profile_avoid_terms(topic_profile)
            elif active_intent:
                pub_id = (active_intent.get("profile") or {}).get("id")
                if pub_id:
                    publisher_brand = str(pub_id)

            today = (output.generated_at or datetime.now()).strftime("%Y-%m-%d")
            batch_path = str(_home() / "hotspots" / f"{today}.json")
            top_k_val = max(1, min(int(gate_a_top_k or 3), 10))
            _triggers.post_gate_a(
                hotspots=[h.to_dict() for h in output.hotspots],
                batch_path=batch_path,
                publisher_brand=publisher_brand,
                top_k=top_k_val,
                avoid_terms=avoid,
                publisher_account=pub or None,
                filter_meta={
                    **(filter_meta or {}),
                    "signals": total_before,
                    **({"recall": recall_meta} if recall_meta else {}),
                    **({"rerank": rerank_meta} if rerank_meta else {}),
                }
                if filter_meta
                else None,
                config_suggestions=config_suggestions,
            )
        except Exception as _err:  # pragma: no cover
            click.echo(f"(Gate A auto-post skipped: {_err})", err=True)

    if as_json:
        payload = output.to_dict()
        if filter_meta:
            payload["filter"] = filter_meta
        if recall_meta:
            payload["recall"] = recall_meta
        if rerank_meta:
            payload["rerank"] = rerank_meta
        _emit_json(payload)
        return

    summary = {
        "generated_at": output.generated_at.isoformat()
        if output.generated_at
        else None,
        "hotspot_count": len(output.hotspots),
        "first_topic_one_liners": [
            h.topic_one_liner for h in output.hotspots[:3]
        ],
    }
    if filter_meta:
        summary["filter"] = filter_meta
    if recall_meta:
        summary["recall"] = recall_meta
    if rerank_meta:
        summary["rerank"] = rerank_meta
    if resolved_profile_id and topic_profile is not None:
        summary["profile"] = {
            "id": resolved_profile_id,
            "label": _profile_meta(resolved_profile_id, topic_profile)["profile_label"],
        }
    click.echo(_json.dumps(summary, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# af search (topic-targeted D1 via HN Algolia)
# ---------------------------------------------------------------------------


@cli.command(
    "search",
    help="Topic-targeted D1 search via HN Algolia. Output is saved as "
    "~/.agentflow/search_results/search_<slug>_<ts>.json (separate from daily scan).",
)
@click.argument("query", required=False)
@click.option(
    "--profile",
    "topic_profile_id",
    type=str,
    default=None,
    help="Reusable topic profile id from ~/.agentflow/topic_profiles.yaml. "
    "If query is omitted, the profile's default_search_query is used.",
)
@click.option(
    "--days",
    type=int,
    default=7,
    show_default=True,
    help="Only include stories from the last N days.",
)
@click.option(
    "--min-points",
    type=int,
    default=10,
    show_default=True,
    help="Drop HN stories below this score.",
)
@click.option(
    "--target-candidates",
    type=int,
    default=10,
    show_default=True,
    help="Upper bound on hotspots to emit after clustering.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit the full D1Output dict as JSON on stdout.",
)
def search(
    query: str | None,
    topic_profile_id: str | None,
    days: int,
    min_points: int,
    target_candidates: int,
    as_json: bool,
) -> None:
    """Run a query-driven D1 pass via HN Algolia, reuse clustering + mining."""
    from agentflow.agent_d1.search import run_d1_search
    from agentflow.shared.memory import append_memory_event
    from agentflow.shared.topic_profile_learning import suggest_from_search

    _, topic_profile = _resolve_topic_profile(topic_profile_id, allow_missing=True)
    active_intent = _load_current_intent_safe()
    effective_query = (query or "").strip()
    profile_queries: list[str] = []
    if not effective_query and topic_profile is not None:
        from agentflow.shared.topic_profiles import topic_profile_search_queries

        profile_queries = topic_profile_search_queries(
            topic_profile,
            topic_profile_id or "",
        )
        if profile_queries:
            effective_query = profile_queries[0]
    if not effective_query:
        if active_intent is not None:
            from agentflow.shared.memory import intent_query_text

            effective_query = intent_query_text(active_intent)
    if not effective_query:
        raise click.UsageError("search requires <query> or --profile (or an active intent).")

    executed_queries = [effective_query]
    if profile_queries and not query:
        executed_queries = _dedupe_keep_order(profile_queries)

    if len(executed_queries) == 1:
        output, saved_path = asyncio.run(
            run_d1_search(
                query=effective_query,
                days=days,
                min_points=min_points,
                target_candidates=target_candidates,
            )
        )
        saved_paths = [saved_path]
    else:
        outputs = []
        for q in executed_queries:
            outputs.append(
                (
                    q,
                    *asyncio.run(
                        run_d1_search(
                            query=q,
                            days=days,
                            min_points=min_points,
                            target_candidates=target_candidates,
                        )
                    ),
                )
            )
        output = _merge_profile_search_outputs(
            outputs,
            target_candidates=target_candidates,
        )
        slug = f"profile_{topic_profile_id or 'query_bundle'}"
        saved_path = _save_search_output(
            output,
            slug=slug,
            extra={
                "search_context": {
                    "queries": executed_queries,
                    "days": days,
                    "min_points": min_points,
                    "target_candidates": target_candidates,
                    "source": "topic_profile" if topic_profile_id else "cli_flag",
                }
            },
        )
        saved_paths = [path for _, _, path in outputs] + [saved_path]

    append_memory_event(
        "topic_intent_used",
        article_id=None,
        payload={
            "command": "search",
            "mode": "search_bundle" if len(executed_queries) > 1 else "search",
            "query": effective_query,
            "queries": executed_queries,
                "source": (
                    "topic_profile"
                    if topic_profile_id and not query
                    else "current_intent"
                    if not query and not topic_profile_id
                    else "cli_flag"
                ),
            "days": days,
            "min_points": min_points,
            "hotspot_count": len(output.hotspots),
            "saved_path": str(saved_path),
            "saved_paths": [str(path) for path in saved_paths],
                **_profile_meta(topic_profile_id, topic_profile),
        },
    )

    active_profile_id = _active_profile_id(topic_profile_id, active_intent)
    _maybe_trigger_profile_setup(active_profile_id, reason="search")
    if active_profile_id:
        try:
            suggestion = suggest_from_search(
                profile_id=active_profile_id,
                queries=executed_queries,
                hotspot_count=len(output.hotspots),
            )
            if suggestion:
                append_memory_event(
                    "topic_profile_suggestion_created",
                    payload={
                        "profile_id": active_profile_id,
                        "suggestion_id": suggestion["id"],
                        "stage": "search",
                    },
                )
        except Exception:
            pass

    if as_json:
        _emit_json(output.to_dict())
        return

    summary = {
        "query": effective_query,
        "queries": executed_queries,
        "days": days,
        "min_points": min_points,
        "hotspot_count": len(output.hotspots),
        "first_topic_one_liners": [h.topic_one_liner for h in output.hotspots[:3]],
        "saved_path": str(saved_path),
    }
    if topic_profile_id and topic_profile is not None:
        summary["profile"] = {
            "id": topic_profile_id,
            "label": _profile_meta(topic_profile_id, topic_profile)["profile_label"],
        }
    click.echo(_json.dumps(summary, ensure_ascii=False, indent=2))

    if not output.hotspots:
        click.echo(
            f"search: 0 hotspots for query={query!r}. Try a broader term, "
            f"longer --days window, or lower --min-points.",
            err=True,
        )


# ---------------------------------------------------------------------------
# af hotspot-show
# ---------------------------------------------------------------------------


@cli.command("hotspot-show", help="Print a full hotspot record (JSON) by id.")
@click.argument("hotspot_id")
@click.option(
    "--date",
    "date",
    type=str,
    default=None,
    help="Restrict the lookup to ~/.agentflow/hotspots/<date>.json (YYYY-MM-DD).",
)
def hotspot_show(hotspot_id: str, date: str | None) -> None:
    hs = _find_hotspot(hotspot_id, date=date)
    _emit_json(hs)


# ---------------------------------------------------------------------------
# af write
# ---------------------------------------------------------------------------


@cli.command("write", help="Start Agent D2 from a hotspot id.")
@click.argument("hotspot_id")
@click.option("--angle", "angle_index", type=int, default=0, show_default=True)
@click.option("--series", "target_series", type=str, default="A", show_default=True)
@click.option(
    "--auto-pick",
    is_flag=True,
    default=False,
    help="Auto-pick title/opening/closing 0 and fill all sections.",
)
@click.option(
    "--title",
    "title_index_override",
    type=int,
    default=None,
    help="Override title index (forces hardcoded path instead of preferences).",
)
@click.option(
    "--opening",
    "opening_index_override",
    type=int,
    default=None,
    help="Override opening index (forces hardcoded path instead of preferences).",
)
@click.option(
    "--closing",
    "closing_index_override",
    type=int,
    default=None,
    help="Override closing index (forces hardcoded path instead of preferences).",
)
@click.option(
    "--ignore-prefs",
    is_flag=True,
    default=False,
    help="Ignore ~/.agentflow/preferences.yaml and force 0/0/0 for --auto-pick.",
)
@click.option("--json", "as_json", is_flag=True, default=False)
def write(
    hotspot_id: str,
    angle_index: int,
    target_series: str,
    auto_pick: bool,
    title_index_override: int | None,
    opening_index_override: int | None,
    closing_index_override: int | None,
    ignore_prefs: bool,
    as_json: bool,
) -> None:
    """Generate a skeleton; optionally auto-fill the whole draft."""
    from agentflow.agent_d2.main import (
        fill_all_sections,
        generate_skeleton_for_hotspot,
    )
    from agentflow.shared import preferences as _prefs_mod
    from agentflow.shared.memory import append_memory_event
    from agentflow.shared.topic_profile_learning import suggest_from_write

    # 1. Validate the hotspot up front so we fail fast with a clear message.
    try:
        hotspot_record = _find_hotspot(hotspot_id)
    except click.ClickException:
        raise

    article_id = _make_article_id(hotspot_id)

    try:
        skeleton = asyncio.run(
            generate_skeleton_for_hotspot(
                hotspot_id=hotspot_id,
                chosen_angle_index=angle_index,
                target_series=target_series,
            )
        )
    except (ValueError, IndexError, KeyError) as err:
        raise click.ClickException(str(err))

    # Persist the skeleton next to the draft so `af fill` can re-use it.
    draft_dir = _draft_dir(article_id)
    draft_dir.mkdir(parents=True, exist_ok=True)
    skeleton_path = draft_dir / "skeleton.json"
    skeleton_dict = skeleton.to_dict()
    skeleton_path.write_text(
        _json.dumps(skeleton_dict, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    # Seed metadata.json so load_draft_metadata works for later commands.
    metadata_path = draft_dir / "metadata.json"
    existing_meta: dict[str, Any] = {}
    if metadata_path.exists():
        try:
            existing_meta = _json.loads(metadata_path.read_text(encoding="utf-8"))
        except Exception:
            existing_meta = {}
    # Snapshot the publisher_account from the active intent so D3/D4 can read
    # it later without depending on intent state (which may be cleared by then).
    from agentflow.shared.memory import load_current_intent
    from agentflow.shared.topic_profiles import resolve_publisher_account_from_intent

    active_intent = load_current_intent()
    publisher_snapshot = resolve_publisher_account_from_intent(active_intent)
    intent_profile = (active_intent.get("profile") or {}) if isinstance(active_intent, dict) else {}

    metadata = {
        **existing_meta,
        "article_id": article_id,
        "hotspot_id": hotspot_id,
        "target_series": target_series,
        "chosen_angle_index": angle_index,
        "skeleton": skeleton_dict,
        "created_at": _now_iso(),
        "status": "skeleton_ready",
    }
    if intent_profile:
        metadata["intent_profile"] = intent_profile
    if publisher_snapshot:
        metadata["publisher_account"] = publisher_snapshot
    metadata_path.write_text(
        _json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    append_memory_event(
        "article_created",
        article_id=article_id,
        hotspot_id=hotspot_id,
        payload={
            "hotspot_id": hotspot_id,
            "angle_index": angle_index,
            "target_series": target_series,
            "auto_filled": bool(auto_pick),
        },
    )

    active_profile_id = str(intent_profile.get("id") or "").strip()
    if active_profile_id:
        try:
            suggestion = suggest_from_write(
                profile_id=active_profile_id,
                hotspot=hotspot_record,
                article_id=article_id,
                draft_title=None,
            )
            if suggestion:
                append_memory_event(
                    "topic_profile_suggestion_created",
                    article_id=article_id,
                    hotspot_id=hotspot_id,
                    payload={
                        "profile_id": active_profile_id,
                        "suggestion_id": suggestion["id"],
                        "stage": "write",
                    },
                )
        except Exception:
            pass

    draft_dict: dict[str, Any] | None = None
    chosen_title_idx = 0
    chosen_opening_idx = 0
    chosen_closing_idx = 0
    defaults_source = "hardcoded"
    defaults_source_events: int | None = None
    defaults_confidence: float | None = None
    any_explicit_override = any(
        v is not None
        for v in (
            title_index_override,
            opening_index_override,
            closing_index_override,
        )
    )
    if auto_pick:
        # Consult preferences only when --auto-pick is on AND no explicit
        # index override was passed AND --ignore-prefs was not set.
        if not ignore_prefs and not any_explicit_override:
            prefs_data = _prefs_mod.get_defaults()
            picked = _prefs_mod.pick_write_indices(prefs_data)
            write_section = (
                prefs_data.get("write") if isinstance(prefs_data, dict) else None
            )
            if any(v is not None for v in picked.values()):
                defaults_source = "preferences"
                if isinstance(write_section, dict):
                    defaults_source_events = write_section.get("_source_events")
                    defaults_confidence = write_section.get("_confidence")
                if picked["title_idx"] is not None:
                    chosen_title_idx = int(picked["title_idx"])
                if picked["opening_idx"] is not None:
                    chosen_opening_idx = int(picked["opening_idx"])
                if picked["closing_idx"] is not None:
                    chosen_closing_idx = int(picked["closing_idx"])

        # Apply explicit CLI overrides last so they trump preferences.
        if title_index_override is not None:
            chosen_title_idx = title_index_override
            defaults_source = "hardcoded"
        if opening_index_override is not None:
            chosen_opening_idx = opening_index_override
            defaults_source = "hardcoded"
        if closing_index_override is not None:
            chosen_closing_idx = closing_index_override
            defaults_source = "hardcoded"

        if not as_json:
            if defaults_source == "preferences":
                click.echo(
                    f"using historical default: title={chosen_title_idx} "
                    f"opening={chosen_opening_idx} closing={chosen_closing_idx} "
                    f"(based on {defaults_source_events} past runs, "
                    f"confidence {defaults_confidence})",
                    err=True,
                )
            else:
                # Only mention "no preferences yet" when user didn't pass
                # --ignore-prefs or explicit overrides; otherwise stay quiet.
                if not ignore_prefs and not any_explicit_override:
                    click.echo("no preferences yet, using 0/0/0", err=True)

        try:
            draft = asyncio.run(
                fill_all_sections(
                    skeleton=skeleton,
                    chosen_title=chosen_title_idx,
                    chosen_opening=chosen_opening_idx,
                    chosen_closing=chosen_closing_idx,
                    style_profile=_load_style_profile_safe(),
                    article_id=article_id,
                )
            )
        except (ValueError, IndexError) as err:
            raise click.ClickException(str(err))
        draft_dict = draft.to_dict()
        append_memory_event(
            "fill_choices",
            article_id=article_id,
            hotspot_id=hotspot_id,
            payload={
                "chosen_title_index": chosen_title_idx,
                "chosen_opening_index": chosen_opening_idx,
                "chosen_closing_index": chosen_closing_idx,
                "mode": "auto_default_indices",
                "defaults_source": defaults_source,
            },
        )
        # Auto-pick path runs fill in-process (bypassing the `af fill` CLI),
        # so the Gate B post-trigger has to be wired here too.
        try:
            from agentflow.agent_review import triggers as _triggers
            gate_summary = _triggers.post_gate_b(article_id)
            if gate_summary and not as_json:
                click.echo(
                    f"Gate B card posted: short_id={gate_summary['short_id']} "
                    f"blockers={gate_summary['blockers']}",
                    err=True,
                )
        except Exception as _err:  # pragma: no cover
            click.echo(f"(Gate B auto-post skipped: {_err})", err=True)

    if as_json:
        payload: dict[str, Any] = {
            "article_id": article_id,
            "hotspot_id": hotspot_id,
            "angle_index": angle_index,
            "target_series": target_series,
            "skeleton": skeleton_dict,
            "draft": draft_dict,
            "auto_filled": bool(auto_pick),
        }
        if auto_pick:
            payload["defaults_source"] = defaults_source
            payload["defaults_source_events"] = defaults_source_events
            payload["chosen_title_index"] = chosen_title_idx
            payload["chosen_opening_index"] = chosen_opening_idx
            payload["chosen_closing_index"] = chosen_closing_idx
        _emit_json(payload)
        return

    click.echo(f"article_id: {article_id}")
    click.echo(
        f"skeleton:   {len(skeleton_dict.get('title_candidates') or [])} titles, "
        f"{len(skeleton_dict.get('opening_candidates') or [])} openings, "
        f"{len(skeleton_dict.get('section_outline') or [])} sections, "
        f"{len(skeleton_dict.get('closing_candidates') or [])} closings"
    )
    if auto_pick and draft_dict is not None:
        click.echo(
            f"draft:      {len(draft_dict.get('sections') or [])} sections, "
            f"{draft_dict.get('total_word_count')} words, "
            f"{len(draft_dict.get('image_placeholders') or [])} image placeholders"
        )
    else:
        click.echo(
            "next:       af fill "
            f"{article_id} --title N --opening N --closing N"
        )


# ---------------------------------------------------------------------------
# af fill
# ---------------------------------------------------------------------------


@cli.command("fill", help="Fill all sections of a skeleton-only draft.")
@click.argument("article_id")
@click.option("--title", "chosen_title", type=int, required=True)
@click.option("--opening", "chosen_opening", type=int, required=True)
@click.option("--closing", "chosen_closing", type=int, required=True)
@click.option("--json", "as_json", is_flag=True, default=False)
def fill(
    article_id: str,
    chosen_title: int,
    chosen_opening: int,
    chosen_closing: int,
    as_json: bool,
) -> None:
    from agentflow.agent_d2.main import fill_all_sections
    from agentflow.shared.memory import append_memory_event
    from agentflow.shared.models import SkeletonOutput

    draft_dir = _draft_dir(article_id)
    skeleton_path = draft_dir / "skeleton.json"
    metadata_path = draft_dir / "metadata.json"

    skeleton_data: dict[str, Any] | None = None
    if skeleton_path.exists():
        try:
            skeleton_data = _json.loads(skeleton_path.read_text(encoding="utf-8"))
        except Exception as err:
            raise click.ClickException(f"Could not parse {skeleton_path}: {err}")
    elif metadata_path.exists():
        try:
            meta = _json.loads(metadata_path.read_text(encoding="utf-8"))
        except Exception as err:
            raise click.ClickException(f"Could not parse {metadata_path}: {err}")
        maybe = meta.get("skeleton")
        if isinstance(maybe, dict):
            skeleton_data = maybe

    if skeleton_data is None:
        raise click.ClickException(
            f"No skeleton found for {article_id}. Run `af write <hotspot_id>` first."
        )

    skeleton = SkeletonOutput.from_dict(skeleton_data)

    hotspot_id = ""
    if metadata_path.exists():
        try:
            meta = _json.loads(metadata_path.read_text(encoding="utf-8"))
            hotspot_id = str(meta.get("hotspot_id") or "")
        except Exception:
            hotspot_id = ""

    try:
        draft = asyncio.run(
            fill_all_sections(
                skeleton=skeleton,
                chosen_title=chosen_title,
                chosen_opening=chosen_opening,
                chosen_closing=chosen_closing,
                style_profile=_load_style_profile_safe(),
                article_id=article_id,
            )
        )
    except (ValueError, IndexError) as err:
        raise click.ClickException(str(err))

    # Update status in metadata.json so the queue view reflects the fill.
    if metadata_path.exists():
        try:
            meta = _json.loads(metadata_path.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
        meta.update(
            {
                "status": "draft_ready",
                "chosen_title_index": chosen_title,
                "chosen_opening_index": chosen_opening,
                "chosen_closing_index": chosen_closing,
                "updated_at": _now_iso(),
            }
        )
        metadata_path.write_text(
            _json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    append_memory_event(
        "fill_choices",
        article_id=article_id,
        hotspot_id=hotspot_id,
        payload={
            "chosen_title_index": chosen_title,
            "chosen_opening_index": chosen_opening,
            "chosen_closing_index": chosen_closing,
            "mode": "manual_refill",
        },
    )

    # Auto-trigger Gate B card if TG is configured. Failures here must NOT
    # break the fill command — the draft is on disk regardless.
    try:
        from agentflow.agent_review import triggers as _triggers
        gate_summary = _triggers.post_gate_b(article_id)
        if gate_summary and not as_json:
            click.echo(
                f"Gate B card posted: short_id={gate_summary['short_id']} "
                f"blockers={gate_summary['blockers']}",
                err=True,
            )
    except Exception as _err:  # pragma: no cover
        click.echo(f"(Gate B auto-post skipped: {_err})", err=True)

    draft_dict = draft.to_dict()
    if as_json:
        _emit_json(draft_dict)
        return

    click.echo(f"article_id: {article_id}")
    click.echo(f"title:      {draft.title}")
    click.echo(f"sections:   {len(draft.sections)}")
    click.echo(f"words:      {draft.total_word_count}")
    click.echo(f"images:     {len(draft.image_placeholders)} placeholder(s)")


# ---------------------------------------------------------------------------
# af edit
# ---------------------------------------------------------------------------


@cli.command(
    "edit",
    help="Apply a natural-language edit to a section / title / opening / closing.",
)
@click.argument("article_id")
@click.option(
    "--target",
    type=click.Choice(["section", "title", "opening", "closing"]),
    default="section",
    show_default=True,
    help="Which scope to edit. Defaults to 'section' for back-compat.",
)
@click.option("--section", "section_index", type=int, default=None)
@click.option("--paragraph", "paragraph_index", type=int, default=None)
@click.option("--command", "edit_command", type=str, required=True)
@click.option("--json", "as_json", is_flag=True, default=False)
def edit(
    article_id: str,
    target: str,
    section_index: int | None,
    paragraph_index: int | None,
    edit_command: str,
    as_json: bool,
) -> None:
    from agentflow.agent_d2.main import apply_user_edit
    from agentflow.shared.memory import append_memory_event

    metadata_path = _draft_dir(article_id) / "metadata.json"
    if not metadata_path.exists():
        raise click.ClickException(
            f"No draft metadata for {article_id}. Run `af write` / `af fill` first."
        )

    if target == "section":
        if section_index is None:
            raise click.ClickException("--section is required when --target=section")
        try:
            new_section = asyncio.run(
                apply_user_edit(
                    article_id=article_id,
                    section_index=section_index,
                    paragraph_index=paragraph_index,
                    command=edit_command,
                )
            )
        except (ValueError, IndexError) as err:
            raise click.ClickException(str(err))
        except FileNotFoundError as err:
            raise click.ClickException(str(err))

        # Touch the metadata timestamp (best-effort).
        try:
            meta = _json.loads(metadata_path.read_text(encoding="utf-8"))
            meta["status"] = "draft_ready"
            meta["updated_at"] = _now_iso()
            metadata_path.write_text(
                _json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            hotspot_id = str(meta.get("hotspot_id") or "")
        except Exception:
            hotspot_id = ""

        append_memory_event(
            "section_edit",
            article_id=article_id,
            hotspot_id=hotspot_id,
            payload={
                "section_index": section_index,
                "paragraph_index": paragraph_index,
                "command": edit_command,
            },
        )

        section_dict = new_section.to_dict()
        if as_json:
            _emit_json(section_dict)
            return

        click.echo(f"article_id:   {article_id}")
        click.echo(f"section:      [{section_index}] {new_section.heading}")
        if paragraph_index is not None:
            click.echo(f"paragraph:    {paragraph_index}")
        click.echo(f"command:      {edit_command}")
        click.echo(f"word_count:   {new_section.word_count}")
        return

    # Meta edit branch: title / opening / closing.
    from agentflow.agent_d2.interactive_editor import (
        edit_closing as _edit_closing,
        edit_opening as _edit_opening,
        edit_title as _edit_title,
    )
    from agentflow.config.style_loader import load_style_profile

    try:
        meta_before = _json.loads(metadata_path.read_text(encoding="utf-8")) or {}
    except Exception:
        meta_before = {}
    old_value = str(meta_before.get(target) or "")

    style_profile = load_style_profile()
    handler = {
        "title": _edit_title,
        "opening": _edit_opening,
        "closing": _edit_closing,
    }[target]

    try:
        new_value = asyncio.run(
            handler(
                article_id=article_id,
                command=edit_command,
                style_profile=style_profile,
            )
        )
    except (ValueError, IndexError) as err:
        raise click.ClickException(str(err))
    except FileNotFoundError as err:
        raise click.ClickException(str(err))

    try:
        meta = _json.loads(metadata_path.read_text(encoding="utf-8"))
        meta["status"] = "draft_ready"
        meta["updated_at"] = _now_iso()
        metadata_path.write_text(
            _json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        hotspot_id = str(meta.get("hotspot_id") or "")
    except Exception:
        hotspot_id = ""

    append_memory_event(
        "meta_edit",
        article_id=article_id,
        hotspot_id=hotspot_id,
        payload={"target": target, "command": edit_command},
    )

    if as_json:
        _emit_json({"target": target, "old": old_value, "new": new_value})
        return

    click.echo(f"article_id: {article_id}")
    click.echo(f"target:     {target}")
    click.echo(f"old:        {old_value}")
    click.echo(f"new:        {new_value}")


# ---------------------------------------------------------------------------
# af preview
# ---------------------------------------------------------------------------


@cli.command("preview", help="Run D3 adapters and persist platform_versions.")
@click.argument("article_id")
@click.option(
    "--platforms",
    "platforms",
    type=str,
    default=None,
    show_default=False,
    help="Comma-separated platforms. Defaults to historical preference "
    "(from preferences.yaml) or 'ghost_wordpress,linkedin_article'.",
)
@click.option(
    "--ignore-prefs",
    is_flag=True,
    default=False,
    help="Ignore preferences.yaml when deciding default platforms.",
)
@click.option("--force-strip-images", is_flag=True, default=False)
@click.option(
    "--skip-images",
    is_flag=True,
    default=False,
    help="Render the platform versions with NO body images: clear all "
    "image_placeholders before D3 runs, and force-strip any [IMAGE: ...] "
    "markers. Cover (D4 cover_image_path) is unaffected since it is sourced "
    "from metadata, not body markdown.",
)
@click.option("--json", "as_json", is_flag=True, default=False)
def preview(
    article_id: str,
    platforms: str | None,
    ignore_prefs: bool,
    force_strip_images: bool,
    skip_images: bool,
    as_json: bool,
) -> None:
    from agentflow.agent_d2.main import load_draft
    from agentflow.agent_d3.main import adapt_all
    from agentflow.shared import preferences as _prefs_mod
    from agentflow.shared.memory import append_memory_event

    try:
        draft = load_draft(article_id)
    except FileNotFoundError as err:
        raise click.ClickException(str(err))

    if skip_images:
        # The cover_image_path used by D4 is read from metadata (placeholder
        # with role='cover'), so we keep cover-role placeholders on the model
        # but clear them from the body-rendering pass: pass an empty list to
        # the adapters by mutating a shallow copy of the draft.
        draft.image_placeholders = []
        force_strip_images = True

    _FALLBACK_PLATFORMS = "ghost_wordpress,linkedin_article"
    target_platforms: list[str]
    if platforms:
        target_platforms = [p.strip() for p in platforms.split(",") if p.strip()]
    else:
        # No --platforms given: consult preferences first, else fallback.
        prefs_platforms: list[str] | None = None
        if not ignore_prefs:
            prefs_platforms = _prefs_mod.pick_preview_platforms()
        if prefs_platforms:
            target_platforms = list(prefs_platforms)
            if not as_json:
                click.echo(
                    f"using historical default platforms: "
                    f"{','.join(target_platforms)}",
                    err=True,
                )
        else:
            target_platforms = [
                p.strip() for p in _FALLBACK_PLATFORMS.split(",") if p.strip()
            ]
    if not target_platforms:
        raise click.ClickException("--platforms must list at least one platform")

    metadata_path = _draft_dir(article_id) / "metadata.json"
    series = "A"
    hotspot_id = ""
    if metadata_path.exists():
        try:
            meta = _json.loads(metadata_path.read_text(encoding="utf-8"))
            series = str(meta.get("target_series") or "A")
            hotspot_id = str(meta.get("hotspot_id") or "")
        except Exception:
            pass

    try:
        d3 = asyncio.run(
            adapt_all(
                draft=draft,
                platforms=target_platforms,
                series=series,
                force_strip_unresolved_images=force_strip_images,
            )
        )
    except Exception as err:
        raise click.ClickException(f"preview failed: {err}")

    # Persist the combined D3Output so `af publish` has a single source of truth.
    d3_path = _draft_dir(article_id) / "d3_output.json"
    d3_dict = d3.to_dict()
    d3_path.write_text(
        _json.dumps(d3_dict, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Update metadata status.
    if metadata_path.exists():
        try:
            meta = _json.loads(metadata_path.read_text(encoding="utf-8"))
            meta["status"] = "preview_ready"
            meta["last_previewed_platforms"] = target_platforms
            meta["updated_at"] = _now_iso()
            metadata_path.write_text(
                _json.dumps(meta, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    append_memory_event(
        "preview",
        article_id=article_id,
        hotspot_id=hotspot_id,
        payload={
            "platforms": target_platforms,
            "force_strip_unresolved_images": force_strip_images,
            "skip_images": skip_images,
        },
    )

    if as_json:
        _emit_json(d3_dict)
        return

    click.echo(f"article_id: {article_id}")
    for v in d3.platform_versions:
        click.echo(
            f"  {v.platform:<22} "
            f"{len(v.content)} chars, "
            f"{len(v.formatting_changes)} formatting changes"
        )


# ---------------------------------------------------------------------------
# af publish
# ---------------------------------------------------------------------------


def _collect_unresolved_placeholders(draft: Any) -> list[dict[str, Any]]:
    unresolved: list[dict[str, Any]] = []
    for ph in getattr(draft, "image_placeholders", []) or []:
        if not ph.resolved_path:
            unresolved.append(
                {
                    "id": ph.id,
                    "description": ph.description,
                    "section_heading": ph.section_heading,
                }
            )
    return unresolved


@cli.command("publish", help="Run D4 (multi-platform publisher) for an article_id.")
@click.argument("article_id")
@click.option(
    "--platforms",
    "platforms",
    type=str,
    default=None,
    help="Override platforms. Default = whatever's in d3_output.json.",
)
@click.option("--force-strip-images", is_flag=True, default=False)
@click.option("--json", "as_json", is_flag=True, default=False)
def publish(
    article_id: str,
    platforms: str | None,
    force_strip_images: bool,
    as_json: bool,
) -> None:
    from agentflow.agent_d2.main import load_draft
    from agentflow.agent_d4.main import publish_all
    from agentflow.config.accounts_loader import load_publishing_credentials
    from agentflow.shared import preferences as _prefs_mod
    from agentflow.shared.memory import append_memory_event
    from agentflow.shared.models import D3Output, PlatformVersion

    # --- preferences: honor ghost_status_override if remaining runs > 0 ---
    override = _prefs_mod.pick_ghost_status_override()
    override_applied = False
    if override:
        pub_section = _prefs_mod.get_defaults().get("publish") or {}
        remaining = pub_section.get("override_remaining_runs")
        click.echo(
            f"[prefs] recent rollback detected — forcing GHOST_STATUS=draft "
            f"for this publish ({remaining} remaining)",
            err=True,
        )
        os.environ["GHOST_STATUS"] = "draft"
        override_applied = True

    drafts_dir = _draft_dir(article_id)
    if not drafts_dir.exists():
        raise click.ClickException(f"publish: draft directory not found: {drafts_dir}")

    # --- scan for unresolved [IMAGE:] placeholders ---
    try:
        draft = load_draft(article_id)
    except FileNotFoundError:
        draft = None

    if draft is not None:
        unresolved = _collect_unresolved_placeholders(draft)
        if unresolved and not force_strip_images:
            lines = [
                f"  - {u['id']}: {u['description']}" for u in unresolved
            ]
            raise click.ClickException(
                "Unresolved image placeholders present. Resolve them with "
                "`af image-resolve` or re-run with --force-strip-images.\n"
                + "\n".join(lines)
            )

    versions: list[PlatformVersion] = []
    d3_json = drafts_dir / "d3_output.json"
    if d3_json.exists():
        data = _json.loads(d3_json.read_text(encoding="utf-8"))
        d3 = D3Output.from_dict(data)
        versions = d3.platform_versions
    else:
        for candidate in sorted(drafts_dir.glob("platform_*.json")):
            versions.append(
                PlatformVersion.from_dict(
                    _json.loads(candidate.read_text(encoding="utf-8"))
                )
            )

    if platforms:
        wanted = {p.strip() for p in platforms.split(",") if p.strip()}
        versions = [v for v in versions if v.platform in wanted]

    if not versions:
        raise click.ClickException(
            f"publish: no platform versions found in {drafts_dir}. "
            "Run `af preview` first."
        )

    d3 = D3Output(article_id=article_id, platform_versions=versions)
    credentials = load_publishing_credentials()
    results = asyncio.run(publish_all(article_id, d3, credentials))

    # memory + metadata.
    metadata_path = drafts_dir / "metadata.json"
    hotspot_id = ""
    if metadata_path.exists():
        try:
            meta = _json.loads(metadata_path.read_text(encoding="utf-8"))
            hotspot_id = str(meta.get("hotspot_id") or "")
            published_platforms = [
                r.platform for r in results if r.status == "success"
            ]
            meta["published_platforms"] = list(
                {*meta.get("published_platforms", []), *published_platforms}
            )
            if published_platforms:
                meta["status"] = "published"
                meta["published_at"] = _now_iso()
            meta["updated_at"] = _now_iso()
            metadata_path.write_text(
                _json.dumps(meta, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    results_payload = [r.to_dict() for r in results]
    append_memory_event(
        "publish",
        article_id=article_id,
        hotspot_id=hotspot_id,
        payload={
            "force_strip_images": force_strip_images,
            "results": results_payload,
            "ghost_status_override_applied": override_applied,
        },
    )

    # If we applied the Ghost override AND Ghost actually succeeded on this
    # run, decrement the remaining-runs counter.
    if override_applied:
        ghost_ok = any(
            getattr(r, "platform", "") == "ghost_wordpress"
            and getattr(r, "status", "") == "success"
            for r in results
        )
        if ghost_ok:
            try:
                _prefs_mod.decrement_override()
            except Exception as err:
                click.echo(
                    f"[prefs] warning: failed to decrement override: {err}",
                    err=True,
                )

    if as_json:
        _emit_json(
            {
                "article_id": article_id,
                "results": results_payload,
            }
        )
        return

    click.echo(f"\npublished {article_id}:")
    click.echo(f"  {'PLATFORM':<24} {'STATUS':<10} URL / REASON")
    click.echo(f"  {'-' * 24} {'-' * 10} {'-' * 40}")
    for r in results:
        url_or_reason = r.published_url or r.failure_reason or "-"
        click.echo(f"  {r.platform:<24} {r.status:<10} {url_or_reason}")


# ---------------------------------------------------------------------------
# af publish-rollback
# ---------------------------------------------------------------------------


@cli.command(
    "publish-rollback",
    help="Delete a previously published post from a platform (Ghost only in v0.1).",
)
@click.argument("article_id")
@click.option(
    "--platform",
    default="ghost_wordpress",
    show_default=True,
    help="Platform whose publish record should be rolled back.",
)
@click.option(
    "--post-id",
    "post_id_override",
    default=None,
    help="Override platform_post_id (for historical records that lack it).",
)
@click.option("--json", "as_json", is_flag=True, default=False)
def publish_rollback(
    article_id: str,
    platform: str,
    post_id_override: str | None,
    as_json: bool,
) -> None:
    from agentflow.agent_d4.publishers.ghost import GhostPublisher
    from agentflow.agent_d4.storage import append_rollback_record, read_publish_history
    from agentflow.shared.memory import append_memory_event

    post_id = post_id_override
    published_url: str | None = None

    if post_id is None:
        records = read_publish_history(article_id)
        matches = [
            r for r in records
            if r.get("platform") == platform and r.get("status") == "success"
        ]
        if not matches:
            raise click.ClickException(
                f"publish-rollback: no successful {platform} record found "
                f"for {article_id}. Pass --post-id to override."
            )
        latest = matches[0]  # read_publish_history sorts desc by published_at
        post_id = latest.get("platform_post_id")
        published_url = latest.get("published_url")
        if not post_id:
            raise click.ClickException(
                "publish-rollback: matching record has no platform_post_id "
                "(pre-fix history). Pass --post-id <id> explicitly."
            )

    if platform != "ghost_wordpress":
        raise click.ClickException(
            f"publish-rollback: platform {platform!r} is not supported in v0.1."
        )

    publisher = GhostPublisher(credentials={})
    ok, reason = publisher.rollback(post_id)

    append_rollback_record(
        article_id=article_id,
        platform=platform,
        platform_post_id=post_id,
        published_url=published_url,
        failure_reason=None if ok else reason,
    )

    # metadata.json status update on success
    metadata_path = _draft_dir(article_id) / "metadata.json"
    if ok and metadata_path.exists():
        try:
            meta = _json.loads(metadata_path.read_text(encoding="utf-8"))
            pubs = [p for p in meta.get("published_platforms", []) if p != platform]
            meta["published_platforms"] = pubs
            if not pubs:
                meta["status"] = "preview_ready"
                meta.pop("published_at", None)
            meta["updated_at"] = _now_iso()
            metadata_path.write_text(
                _json.dumps(meta, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    append_memory_event(
        "publish_rolled_back",
        article_id=article_id,
        payload={
            "platform": platform,
            "platform_post_id": post_id,
            "ok": ok,
            "failure_reason": reason,
        },
    )

    payload = {
        "article_id": article_id,
        "platform": platform,
        "platform_post_id": post_id,
        "status": "rolled_back" if ok else "rollback_failed",
        "failure_reason": reason,
    }
    if as_json:
        _emit_json(payload)
        return

    if ok:
        click.echo(f"rolled back {platform} post {post_id} for {article_id}")
    else:
        raise click.ClickException(f"rollback failed: {reason}")


# ---------------------------------------------------------------------------
# af image-resolve
# ---------------------------------------------------------------------------


@cli.command(
    "image-resolve",
    help="Attach a local file path to an ImagePlaceholder on a draft.",
)
@click.argument("article_id")
@click.argument("placeholder_id")
@click.argument("file_path", type=click.Path(exists=True, dir_okay=False))
def image_resolve(article_id: str, placeholder_id: str, file_path: str) -> None:
    from agentflow.agent_d2.main import load_draft, save_draft
    from agentflow.shared.memory import append_memory_event

    try:
        draft = load_draft(article_id)
    except FileNotFoundError as err:
        raise click.ClickException(str(err))

    found = False
    for ph in draft.image_placeholders:
        if ph.id == placeholder_id:
            ph.resolved_path = str(Path(file_path).resolve())
            found = True
            break
    if not found:
        raise click.ClickException(
            f"placeholder {placeholder_id!r} not found in article {article_id}"
        )

    save_draft(draft)

    # memory.
    metadata_path = _draft_dir(article_id) / "metadata.json"
    hotspot_id = ""
    if metadata_path.exists():
        try:
            hotspot_id = str(
                (_json.loads(metadata_path.read_text(encoding="utf-8")) or {}).get(
                    "hotspot_id"
                )
                or ""
            )
        except Exception:
            hotspot_id = ""
    append_memory_event(
        "image_resolved",
        article_id=article_id,
        hotspot_id=hotspot_id,
        payload={
            "placeholder_id": placeholder_id,
            "file_path": str(Path(file_path).resolve()),
        },
    )

    remaining = len(_collect_unresolved_placeholders(draft))
    _emit_json({"ok": True, "remaining_unresolved": remaining})


# ---------------------------------------------------------------------------
# af draft-show
# ---------------------------------------------------------------------------


@cli.command("draft-show", help="Show a draft by article_id.")
@click.argument("article_id")
@click.option("--json", "as_json", is_flag=True, default=False)
def draft_show(article_id: str, as_json: bool) -> None:
    from agentflow.agent_d2.main import load_draft

    try:
        draft = load_draft(article_id)
    except FileNotFoundError as err:
        raise click.ClickException(str(err))

    if as_json:
        _emit_json(draft.to_dict())
        return

    click.echo(f"article_id: {draft.article_id}")
    click.echo(f"title:      {draft.title}")
    click.echo(f"total:      {draft.total_word_count} words")
    for i, section in enumerate(draft.sections):
        click.echo(
            f"  [{i}] {section.heading:<40} {section.word_count} words"
        )
    unresolved = _collect_unresolved_placeholders(draft)
    click.echo(
        f"images:     {len(draft.image_placeholders)} placeholder(s), "
        f"{len(unresolved)} unresolved"
    )


# ---------------------------------------------------------------------------
# af memory-tail
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# af intent-set / intent-show / intent-clear
# ---------------------------------------------------------------------------


def _intents_path() -> Path:
    from agentflow.shared.bootstrap import agentflow_home, ensure_user_dirs

    ensure_user_dirs()
    d = agentflow_home() / "intents"
    d.mkdir(parents=True, exist_ok=True)
    return d / "current.yaml"


@cli.command(
    "intent-set",
    help="Set the current TopicIntent — subsequent af hotspots/search "
    "read it as a default filter. See docs/backlog/TOPIC_INTENT_FRAMEWORK.md.",
)
@click.argument("query", required=False)
@click.option(
    "--profile",
    "topic_profile_id",
    type=str,
    default=None,
    help="Reusable topic profile id from ~/.agentflow/topic_profiles.yaml. "
    "If query is omitted, the profile's intent text is used.",
)
@click.option(
    "--ttl",
    type=click.Choice(["single_use", "session", "persistent"]),
    default="session",
    show_default=True,
    help="How long the intent persists. "
    "single_use = one command; session = until cleared; persistent = also "
    "remembered by `af prefs-rebuild` for long-term recall.",
)
@click.option(
    "--mode",
    type=click.Choice(["keyword", "regex", "semantic"]),
    default="keyword",
    show_default=True,
    help="Match mode. semantic requires Jina embeddings (v0.5, not wired yet).",
)
@click.option("--json", "as_json", is_flag=True, default=False)
def intent_set(
    query: str | None,
    topic_profile_id: str | None,
    ttl: str,
    mode: str,
    as_json: bool,
) -> None:
    import yaml
    from agentflow.shared.memory import append_memory_event

    _, topic_profile = _resolve_topic_profile(topic_profile_id, allow_missing=True)
    effective_query = (query or "").strip()
    profile_payload: dict[str, Any] = {}
    if topic_profile is not None:
        from agentflow.shared.topic_profiles import (
            topic_profile_intent_text,
            topic_profile_keywords_payload,
            topic_profile_label,
        )

        if not effective_query:
            effective_query = topic_profile_intent_text(topic_profile, topic_profile_id or "")
        profile_payload = {
            "profile": {
                "id": topic_profile_id,
                "label": topic_profile_label(topic_profile, topic_profile_id or ""),
                "summary": str(topic_profile.get("summary") or "").strip(),
            },
            "keywords": topic_profile_keywords_payload(topic_profile),
        }
    elif topic_profile_id:
        if not effective_query:
            effective_query = topic_profile_id
        profile_payload = {
            "profile": {
                "id": topic_profile_id,
                "label": topic_profile_id,
                "summary": "",
            },
            "keywords": {"expanded": [], "primary": [], "avoid": []},
        }
    if not effective_query:
        raise click.UsageError("intent-set requires <query> or --profile.")

    payload = {
        "schema_version": 1,
        "created_at": _now_iso(),
        "source": "cli_flag",
        "query": {"text": effective_query, "mode": mode},
        "metadata": {"ttl": ttl},
        **profile_payload,
    }

    path = _intents_path()
    with path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(payload, fh, allow_unicode=True, sort_keys=False)

    append_memory_event(
        "topic_intent_set",
        article_id=None,
        payload={
            "query": effective_query,
            "mode": mode,
            "ttl": ttl,
            "source": "cli_flag",
            **(
                {
                    "profile_id": topic_profile_id,
                    "profile_label": (
                        (profile_payload.get("profile") or {}).get("label")
                    ),
                }
                if topic_profile_id
                else {}
            ),
        },
    )
    if topic_profile_id:
        _maybe_trigger_profile_setup(topic_profile_id, reason="intent-set")

    if as_json:
        _emit_json({"ok": True, "path": str(path), "intent": payload})
        return
    click.echo(f"intent set: {effective_query!r}  ttl={ttl}  mode={mode}")
    click.echo(f"  stored at {path}")
    if topic_profile_id:
        click.echo(f"  profile: {topic_profile_id}")
    if ttl == "persistent":
        click.echo(
            "  NOTE: run `af prefs-rebuild` to fold this into "
            "`preferences.intent.persistent_query` for long-term recall."
        )


@cli.command("intent-show", help="Show the current TopicIntent (if any).")
@click.option("--json", "as_json", is_flag=True, default=False)
def intent_show(as_json: bool) -> None:
    import yaml

    path = _intents_path()
    if not path.exists():
        if as_json:
            _emit_json({"intent": None})
            return
        click.echo("no intent set (use `af intent-set <query>`).")
        return

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if as_json:
        _emit_json({"intent": data, "path": str(path)})
        return

    q = (data.get("query") or {}).get("text", "")
    mode = (data.get("query") or {}).get("mode", "?")
    ttl = (data.get("metadata") or {}).get("ttl", "?")
    profile = data.get("profile") or {}
    created = data.get("created_at", "?")
    click.echo(f"current intent:")
    click.echo(f"  query    : {q!r}")
    click.echo(f"  mode     : {mode}")
    click.echo(f"  ttl      : {ttl}")
    if isinstance(profile, dict) and profile.get("id"):
        click.echo(f"  profile  : {profile.get('id')} ({profile.get('label', '')})")
    click.echo(f"  created  : {created}")
    click.echo(f"  file     : {path}")


@cli.command("intent-clear", help="Clear the current TopicIntent.")
@click.option("--json", "as_json", is_flag=True, default=False)
def intent_clear(as_json: bool) -> None:
    from agentflow.shared.memory import append_memory_event

    path = _intents_path()
    existed = path.exists()
    if existed:
        path.unlink()

    append_memory_event(
        "topic_intent_cleared",
        article_id=None,
        payload={"existed": existed},
    )

    if as_json:
        _emit_json({"ok": True, "existed": existed})
        return
    click.echo(
        "intent cleared." if existed else "no intent was set; nothing to clear."
    )


# ---------------------------------------------------------------------------
# af memory-tail
# ---------------------------------------------------------------------------


@cli.command("memory-tail", help="Print the last N memory events (JSONL).")
@click.option("--limit", type=int, default=20, show_default=True)
@click.option("--article-id", "article_id", type=str, default=None)
@click.option("--json", "as_json", is_flag=True, default=False)
def memory_tail(limit: int, article_id: str | None, as_json: bool) -> None:
    from agentflow.shared.memory import memory_events_path

    path = memory_events_path()
    if not path.exists():
        if as_json:
            _emit_json([])
        else:
            click.echo("(no events)")
        return

    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = _json.loads(line)
            except Exception:
                continue
            if article_id and record.get("article_id") != article_id:
                continue
            events.append(record)

    tail = events[-limit:] if limit > 0 else events

    if as_json:
        _emit_json(tail)
        return

    for ev in tail:
        click.echo(
            f"{ev.get('ts')}  {ev.get('event_type'):<18} "
            f"article={ev.get('article_id') or '-'}  "
            f"payload={_json.dumps(ev.get('payload') or {}, ensure_ascii=False)}"
        )


# ---------------------------------------------------------------------------
# af run-once (kept — unchanged behaviour)
# ---------------------------------------------------------------------------


@cli.command("run-once", help="Run D1, then hand off to the Web UI and optionally D4.")
@click.option(
    "--scan-window-hours",
    type=int,
    default=24,
    show_default=True,
)
@click.option(
    "--target-candidates",
    type=int,
    default=20,
    show_default=True,
)
@click.option(
    "--article-id",
    type=str,
    default=None,
    help="Optional article_id to publish after the UI stage if D3 output exists.",
)
@click.option(
    "--publish-ready",
    is_flag=True,
    default=False,
    help="If set with --article-id, publish immediately once D3 output is present.",
)
def run_once(
    scan_window_hours: int,
    target_candidates: int,
    article_id: str | None,
    publish_ready: bool,
) -> None:
    from agentflow.agent_d1.main import run_d1_scan
    from agentflow.shared.bootstrap import agentflow_home

    output = asyncio.run(
        run_d1_scan(
            scan_window_hours=scan_window_hours,
            target_candidates=target_candidates,
        )
    )

    hotspots_path = (
        agentflow_home() / "hotspots" / f"{output.generated_at.strftime('%Y-%m-%d')}.json"
    )
    click.echo("")
    click.echo("run-once: D1 complete")
    click.echo(f"  hotspots file : {hotspots_path}")
    click.echo(f"  hotspot count : {len(output.hotspots)}")
    click.echo("")
    click.echo("next:")
    click.echo("  1. Start the API + frontend if they are not already running.")
    click.echo("  2. Open http://localhost:3000/hotspots for batch review.")
    click.echo("  3. In the UI, generate the draft, edit sections, upload images, and preview.")
    click.echo("  4. Return here with --article-id <id> --publish-ready to trigger D4.")

    if not publish_ready:
        return

    if not article_id:
        raise click.UsageError("--publish-ready requires --article-id.")

    drafts_dir = agentflow_home() / "drafts" / article_id
    if not drafts_dir.exists():
        raise click.ClickException(f"Draft directory not found: {drafts_dir}")

    d3_json = drafts_dir / "d3_output.json"
    platform_dir = drafts_dir / "platform_versions"
    if not d3_json.exists() and not platform_dir.exists():
        raise click.ClickException(
            "No D3 output found yet. Generate previews in the Web UI before publishing."
        )

    click.echo("")
    click.echo(f"run-once: publishing ready article {article_id}")
    # Delegate to the publish command body.
    ctx = click.get_current_context()
    ctx.invoke(
        publish,
        article_id=article_id,
        platforms=None,
        force_strip_images=False,
        as_json=False,
    )


# ---------------------------------------------------------------------------
# Optional command modules — each file self-registers on the ``cli`` group
# via @cli.command decorators. We import them last so every base command is
# defined first. Missing modules are ignored (slice not yet shipped).
# ---------------------------------------------------------------------------

for _mod_name in (
    "agentflow.cli.image_commands",
    "agentflow.cli.medium_commands",
    "agentflow.cli.tweet_commands",
    "agentflow.cli.newsletter_commands",
    "agentflow.cli.prefs_commands",
    "agentflow.cli.report_commands",
    "agentflow.cli.learning_review_commands",
    "agentflow.cli.intent_commands",
    "agentflow.cli.review_commands",
    "agentflow.cli.onboard_commands",
    "agentflow.cli.topic_profile_commands",
    "agentflow.cli.skill_commands",
    "agentflow.cli.bootstrap_commands",
):
    try:
        __import__(_mod_name)
    except ImportError:
        pass


if __name__ == "__main__":  # pragma: no cover
    cli()
