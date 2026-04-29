"""Agent D3 entry point: fan draft out to platform adapters in parallel.

Writes each platform version to:
    ~/.agentflow/drafts/<article_id>/platform_versions/<platform>.md
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from agentflow.agent_d3.adapters import ADAPTERS
from agentflow.agent_d3.platform_rules import load_rules, rules_for
from agentflow.config.style_loader import load_style_profile
from agentflow.shared.bootstrap import agentflow_home, ensure_user_dirs
from agentflow.shared.logger import get_logger
from agentflow.shared.models import DraftOutput, D3Output, PlatformVersion

_log = get_logger("agent_d3.main")


async def adapt_all(
    draft: DraftOutput,
    platforms: list[str],
    series: str = "A",
    force_strip_unresolved_images: bool = False,
    style_profile: dict[str, Any] | None = None,
) -> D3Output:
    """Run all requested platform adapters concurrently.

    Returns a ``D3Output`` and persists each platform version as a ``.md``
    under ``~/.agentflow/drafts/<article_id>/platform_versions/``.

    Unknown platforms (not in v0.1 ADAPTERS map) are skipped with a warning.
    """
    ensure_user_dirs()

    if style_profile is None:
        try:
            style_profile = load_style_profile()
        except Exception as err:
            _log.warning("D3: could not load style profile, using empty dict: %s", err)
            style_profile = {}

    all_rules = load_rules()

    tasks: list[tuple[str, asyncio.Task[PlatformVersion]]] = []
    for platform in platforms:
        if platform not in ADAPTERS:
            _log.warning("D3: platform %s not supported in v0.1, skipping", platform)
            continue
        adapter_cls = ADAPTERS[platform]
        rules = all_rules.get(platform) or rules_for(platform, style_profile)
        # Overlay style-profile emoji density override, if any.
        emoji_override = (
            (style_profile.get("emoji_preferences") or {})
            .get("density_by_platform", {})
            .get(platform)
        )
        if emoji_override:
            rules = dict(rules)
            rules["emoji_density"] = emoji_override

        adapter = adapter_cls(rules, style_profile)
        tasks.append(
            (
                platform,
                asyncio.create_task(
                    adapter.adapt(
                        draft,
                        series=series,
                        force_strip_unresolved_images=force_strip_unresolved_images,
                    )
                ),
            )
        )

    versions: list[PlatformVersion] = []
    errors: list[tuple[str, BaseException]] = []
    for platform, task in tasks:
        try:
            version = await task
            versions.append(version)
        except Exception as err:
            errors.append((platform, err))
            _log.error("D3 adapter failed for %s: %s", platform, err)

    # Persist to disk.
    out_dir = agentflow_home() / "drafts" / draft.article_id / "platform_versions"
    out_dir.mkdir(parents=True, exist_ok=True)
    for version in versions:
        path = out_dir / f"{version.platform}.md"
        _write_platform_md(path, version)

    if errors and not versions:
        # Re-raise the first error if nothing succeeded.
        raise errors[0][1]

    return D3Output(article_id=draft.article_id, platform_versions=versions)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _write_platform_md(path: Path, version: PlatformVersion) -> None:
    """Write a platform version as markdown with a YAML front-matter header.

    The front matter carries metadata + formatting_changes so downstream
    tooling (Agent D4, UI previews) can read everything in one shot.
    """
    import yaml  # local import to keep import graph shallow

    front = {
        "platform": version.platform,
        "metadata": dict(version.metadata),
        "formatting_changes": list(version.formatting_changes),
    }
    header = yaml.safe_dump(
        front,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    ).strip()

    body = f"---\n{header}\n---\n\n{version.content.rstrip()}\n"
    path.write_text(body, encoding="utf-8")
