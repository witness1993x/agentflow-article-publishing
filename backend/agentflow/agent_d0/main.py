"""Agent D0 orchestrator — user-facing entry points.

- ``learn_from_sources``  : ingest a list of files/URLs, write style_profile
- ``show_current``        : print current profile + corpus
- ``learn_from_published``: re-ingest published drafts from publish_history
- ``recompute``           : re-aggregate from the full corpus
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Iterable

import yaml

from agentflow.agent_d0 import aggregator, corpus, extractor, per_article_analyzer
from agentflow.config.style_loader import (
    USER_STYLE_PATH,
    load_style_profile,
    save_style_profile,
)
from agentflow.shared.bootstrap import agentflow_home, ensure_user_dirs
from agentflow.shared.logger import get_logger

_log = get_logger("agent_d0.main")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _previous_generation() -> int:
    try:
        current = load_style_profile()
    except FileNotFoundError:
        return 0
    meta = current.get("_meta") if isinstance(current, dict) else None
    if not isinstance(meta, dict):
        return 0
    gen = meta.get("recompute_generation")
    try:
        return int(gen) if gen is not None else 0
    except (TypeError, ValueError):
        return 0


async def _analyze_many(
    articles: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Concurrent per-article analysis → list of (article, analysis)."""
    tasks = [per_article_analyzer.analyze_article(a) for a in articles]
    results = await asyncio.gather(*tasks, return_exceptions=False)
    return list(zip(articles, results))


def _yaml_dump(data: dict[str, Any]) -> str:
    return yaml.safe_dump(
        data, allow_unicode=True, sort_keys=False, default_flow_style=False
    )


# --------------------------------------------------------------------------- #
# learn_from_sources
# --------------------------------------------------------------------------- #


async def learn_from_sources(
    sources: list[str],
    recompute_all: bool = False,
    *,
    identity_hint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Read + analyze + persist + aggregate. Returns the saved profile dict.

    ``recompute_all=False``: ingest new sources, union them with existing corpus
    for aggregation (so the profile reflects every article we've seen).
    ``recompute_all=True``: same ingestion step but explicitly re-emits using
    every corpus record (the union logic is identical when sources is non-empty,
    but this flag is respected for symmetry with the CLI).
    """
    ensure_user_dirs()

    articles = extractor.extract_sources(sources)

    new_pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    if articles:
        _log.info("Analyzing %d article(s) concurrently…", len(articles))
        new_pairs = await _analyze_many(articles)
        for article, analysis in new_pairs:
            sid = article.get("source_id")
            if not sid:
                _log.warning("Article with no source_id, skipping corpus save")
                continue
            corpus.save_analysis(sid, article, analysis)

    # Union: everything now in corpus (includes the just-written ones).
    all_records = corpus.load_all_records()
    all_analyses = [
        r["analysis"] for r in all_records if isinstance(r.get("analysis"), dict)
    ]
    all_hashes = [
        (r.get("article") or {}).get("source_id")
        for r in all_records
        if (r.get("article") or {}).get("source_id")
    ]

    if not all_analyses:
        raise RuntimeError(
            "No per-article analyses available — supply at least one source."
        )

    generation = _previous_generation() + 1

    _log.info(
        "Aggregating %d analysis records (gen=%d, recompute_all=%s)",
        len(all_analyses),
        generation,
        recompute_all,
    )

    profile = await aggregator.aggregate(
        all_analyses,
        identity_hint=identity_hint,
        source_article_hashes=all_hashes,
        recompute_generation=generation,
    )

    save_style_profile(profile)
    _log.info("Saved style profile to %s", USER_STYLE_PATH)
    return profile


# --------------------------------------------------------------------------- #
# show_current
# --------------------------------------------------------------------------- #


async def show_current() -> dict[str, Any]:
    """Print the current profile + corpus list and return the profile."""
    try:
        profile = load_style_profile()
    except FileNotFoundError:
        print("No style profile found yet. Run `af learn-style --dir ...` first.")
        return {}

    print("=" * 60)
    print(f"Style profile at {USER_STYLE_PATH}")
    print("=" * 60)
    print(_yaml_dump(profile))

    print("=" * 60)
    print("Style corpus entries")
    print("=" * 60)
    entries = corpus.list_corpus()
    if not entries:
        print("(corpus is empty)")
    else:
        for e in entries:
            print(
                f"- {e.get('source_id'):<14} "
                f"[{e.get('source_type') or '?':<5}] "
                f"{e.get('title') or '(no title)'}"
            )
    return profile


# --------------------------------------------------------------------------- #
# learn_from_published
# --------------------------------------------------------------------------- #


async def learn_from_published() -> dict[str, Any]:
    """Re-ingest from ``~/.agentflow/publish_history.jsonl`` → drafts/<id>/draft.md."""
    ensure_user_dirs()
    history_path = agentflow_home() / "publish_history.jsonl"
    drafts_dir = agentflow_home() / "drafts"

    if not history_path.exists():
        raise FileNotFoundError(f"No publish history at {history_path}")

    paths: list[str] = []
    seen: set[str] = set()
    with history_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            article_id = rec.get("article_id") or rec.get("id")
            if not article_id or article_id in seen:
                continue
            seen.add(article_id)
            candidate = drafts_dir / article_id / "draft.md"
            if candidate.exists():
                paths.append(str(candidate))

    if not paths:
        raise RuntimeError("No published drafts found to learn from.")

    return await learn_from_sources(paths, recompute_all=False)


# --------------------------------------------------------------------------- #
# recompute
# --------------------------------------------------------------------------- #


async def recompute() -> dict[str, Any]:
    """Re-aggregate from the full corpus without ingesting anything new."""
    return await learn_from_sources([], recompute_all=True)


# --------------------------------------------------------------------------- #
# Sync CLI entry
# --------------------------------------------------------------------------- #


def run(
    *,
    dir_: str | None = None,
    file_: Iterable[str] | str | None = None,
    url: Iterable[str] | str | None = None,
    from_published: bool = False,
    show: bool = False,
    recompute: bool = False,  # noqa: A002 — matches CLI flag name
) -> dict[str, Any]:
    """Synchronous wrapper used by the click command."""
    files = _as_list(file_)
    urls = _as_list(url)

    if show:
        return asyncio.run(show_current())

    if from_published:
        return asyncio.run(learn_from_published())

    if recompute:
        return asyncio.run(_recompute_entry(extra_sources=extractor.expand_inputs(
            dir_=dir_, files=files, urls=urls
        )))

    sources = extractor.expand_inputs(dir_=dir_, files=files, urls=urls)
    if not sources:
        raise click_usage_error(
            "Provide --dir / --file / --url, or use --show / --recompute / --from-published."
        )

    result = asyncio.run(learn_from_sources(sources))
    print(f"\nSaved style profile to {USER_STYLE_PATH}")
    return result


async def _recompute_entry(extra_sources: list[str]) -> dict[str, Any]:
    """Backend for ``--recompute``: optionally add new sources first, then re-aggregate."""
    if extra_sources:
        return await learn_from_sources(extra_sources, recompute_all=True)
    return await recompute()


def _as_list(val: Iterable[str] | str | None) -> list[str]:
    if val is None:
        return []
    if isinstance(val, str):
        return [val]
    return list(val)


def click_usage_error(msg: str) -> Exception:
    """Avoid importing click at module load; raise a ValueError that CLI catches."""
    return ValueError(msg)
