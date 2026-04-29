"""Shared helpers for hotspot scans and search-result archives."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from agentflow.shared.bootstrap import agentflow_home, ensure_user_dirs

_SCAN_FILE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.json$")


def hotspots_dir() -> Path:
    ensure_user_dirs()
    return agentflow_home() / "hotspots"


def search_results_dir() -> Path:
    ensure_user_dirs()
    return agentflow_home() / "search_results"


def _sorted_json_files(root: Path, pattern: str = "*.json") -> list[Path]:
    if not root.exists():
        return []
    return sorted(
        [path for path in root.glob(pattern) if path.is_file()],
        reverse=True,
    )


def iter_scan_files(limit_days: int | None = 7) -> list[Path]:
    files = [
        path
        for path in _sorted_json_files(hotspots_dir())
        if _SCAN_FILE_RE.match(path.name)
    ]
    return files[:limit_days] if limit_days else files


def iter_search_result_files(*, include_legacy: bool = True) -> list[Path]:
    files = _sorted_json_files(search_results_dir())
    if include_legacy:
        legacy = _sorted_json_files(hotspots_dir(), "search_*.json")
        seen = {path.resolve() for path in files}
        for path in legacy:
            resolved = path.resolve()
            if resolved in seen:
                continue
            files.append(path)
            seen.add(resolved)
    return files


def iter_lookup_files(
    *,
    limit_days: int | None = 7,
    include_search_results: bool = True,
) -> list[Path]:
    files = list(iter_scan_files(limit_days=limit_days))
    if include_search_results:
        files.extend(iter_search_result_files())
    return files


def load_hotspot_container(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_hotspots_from_file(path: Path) -> list[dict[str, Any]]:
    try:
        data = load_hotspot_container(path)
    except Exception:
        return []
    hotspots = data.get("hotspots") or data.get("items") or []
    return hotspots if isinstance(hotspots, list) else []


def find_hotspot_record(
    hotspot_id: str,
    *,
    date: str | None = None,
    limit_days: int | None = 7,
    include_search_results: bool = True,
) -> tuple[dict[str, Any], Path]:
    if date:
        files = [hotspots_dir() / f"{date}.json"]
    else:
        files = iter_lookup_files(
            limit_days=limit_days,
            include_search_results=include_search_results,
        )

    for path in files:
        if not path.exists():
            continue
        for hotspot in load_hotspots_from_file(path):
            if hotspot.get("id") == hotspot_id:
                return hotspot, path

    raise KeyError(hotspot_id)


def load_hotspot_refs(hotspot_id: str) -> list[dict[str, Any]]:
    if not hotspot_id:
        return []
    try:
        hotspot, _ = find_hotspot_record(
            hotspot_id,
            limit_days=None,
            include_search_results=True,
        )
    except KeyError:
        return []
    refs = hotspot.get("source_references") or []
    return list(refs) if isinstance(refs, list) else []
