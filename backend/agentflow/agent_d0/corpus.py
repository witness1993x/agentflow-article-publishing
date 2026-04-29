"""Manage ~/.agentflow/style_corpus/ — per-article analysis records + raw text."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agentflow.shared.bootstrap import agentflow_home, ensure_user_dirs
from agentflow.shared.logger import get_logger

_log = get_logger("agent_d0.corpus")


def _corpus_dir() -> Path:
    ensure_user_dirs()
    return agentflow_home() / "style_corpus"


def _raw_dir() -> Path:
    d = _corpus_dir() / "raw"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_analysis(
    source_id: str,
    article: dict[str, Any],
    analysis: dict[str, Any],
) -> Path:
    """Write ``<corpus>/<source_id>.json`` + ``<corpus>/raw/<source_id>.txt``."""
    if not source_id:
        raise ValueError("source_id is required")

    corpus = _corpus_dir()
    raw = _raw_dir()

    text = article.get("text") or ""
    raw_path = raw / f"{source_id}.txt"
    raw_path.write_text(text, encoding="utf-8")

    record = {
        "article": {
            "title": article.get("title"),
            "source_type": article.get("source_type"),
            "source_id": source_id,
            "source_ref": article.get("source_ref"),
            "text_preview": text[:500],
        },
        "analysis": analysis,
        "ingested_at": datetime.now(timezone.utc).isoformat(),
    }

    out_path = corpus / f"{source_id}.json"
    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(record, fh, ensure_ascii=False, indent=2)

    return out_path


def _iter_records() -> list[dict[str, Any]]:
    corpus = _corpus_dir()
    records: list[dict[str, Any]] = []
    if not corpus.exists():
        return records

    for path in sorted(corpus.glob("*.json")):
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as err:
            _log.warning("Corpus file %s unreadable: %s", path, err)
            continue
        if isinstance(data, dict):
            records.append(data)
    return records


def load_all_analyses() -> list[dict[str, Any]]:
    """Return every stored per-article ``analysis`` dict."""
    return [r["analysis"] for r in _iter_records() if isinstance(r.get("analysis"), dict)]


def load_all_hashes() -> list[str]:
    """Return every stored ``source_id``."""
    out: list[str] = []
    for r in _iter_records():
        art = r.get("article") or {}
        sid = art.get("source_id")
        if sid:
            out.append(sid)
    return out


def list_corpus() -> list[dict[str, Any]]:
    """Return summary metadata for each corpus entry (for ``--show``)."""
    summaries: list[dict[str, Any]] = []
    for r in _iter_records():
        art = r.get("article") or {}
        summaries.append(
            {
                "source_id": art.get("source_id"),
                "title": art.get("title"),
                "source_type": art.get("source_type"),
                "source_ref": art.get("source_ref"),
                "ingested_at": r.get("ingested_at"),
            }
        )
    return summaries


def load_all_records() -> list[dict[str, Any]]:
    """Full records (article + analysis) — used by ``learn_from_sources``
    when it needs the source_id paired with its analysis."""
    return _iter_records()
