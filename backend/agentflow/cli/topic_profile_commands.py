"""`af topic-profile` — manage user topic-profile lifecycle."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import click
import yaml

from agentflow.cli.commands import _emit_json, cli
from agentflow.config.topic_profiles_loader import user_topic_profiles_path
from agentflow.shared.agent_bridge import emit_agent_event
from agentflow.shared.bootstrap import agentflow_home, ensure_user_dirs
from agentflow.shared.memory import append_memory_event
from agentflow.shared.topic_profile_lifecycle import (
    apply_suggestion,
    build_patch_from_answers,
    list_suggestions,
    load_session,
    load_suggestion,
    load_user_topic_profiles,
    make_learning_suggestion,
    materialize_user_topic_profiles,
    review_suggestion,
    seed_profile,
    upsert_profile,
    user_profile_bootstrap_state,
)
from agentflow.shared.topic_profiles import (
    get_topic_profile,
    topic_profile_default_description,
)


def _load_patch_from_file(path: str) -> dict[str, Any]:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise click.ClickException(f"patch file must contain a mapping: {path}")
    return payload


def _resolve_patch(
    *,
    profile_id: str,
    from_file: str | None,
    from_session: str | None,
) -> tuple[dict[str, Any], str]:
    if from_file and from_session:
        raise click.UsageError("pass only one of --from-file or --from-session")
    if from_file:
        return _load_patch_from_file(from_file), f"file:{from_file}"
    if from_session:
        session = load_session(from_session)
        patch = session.get("profile_patch")
        if not isinstance(patch, dict) or not patch:
            answers = session.get("answers") or {}
            if not isinstance(answers, dict):
                raise click.ClickException("session has no usable answers/profile_patch")
            existing = user_profile_bootstrap_state(profile_id).get("current_profile") or seed_profile(profile_id)
            patch = build_patch_from_answers(profile_id, answers, existing_profile=existing)
        return patch, f"session:{from_session}"
    raise click.UsageError("one of --from-file or --from-session is required")


def _interactive_collect_patch(profile_id: str) -> dict[str, Any]:
    """Walk the user through 7 steps and return a profile patch dict.

    All questions accept an empty input (Enter) to skip. Only non-empty answers
    end up in the returned patch — so existing fields aren't blanked.
    """
    click.echo("")
    click.echo(f"== Interactive topic-profile init for '{profile_id}' ==")
    click.echo("All prompts can be skipped with an empty Enter.\n")

    patch: dict[str, Any] = {}
    pub: dict[str, Any] = {}

    # Step 1 — label
    label = click.prompt(
        "Step 1/7  品牌显示名 (label)",
        default=profile_id,
        show_default=True,
    ).strip()
    if label:
        patch["label"] = label

    # Step 2 — default_description (freeform paragraph)
    click.echo("")
    click.echo(
        "Step 2/7  自由描述这个 profile（一段话；AI 据此推 keywords/dos/donts）。"
    )
    click.echo("          回车跳过；或输入 'edit' 调用 $EDITOR 写多行文本。")
    description_input = click.prompt(
        "default_description",
        default="",
        show_default=False,
    )
    if description_input.strip().lower() == "edit":
        edited = click.edit(text="") or ""
        description = edited.strip()
    else:
        description = description_input.strip()
    if description:
        pub["default_description"] = description

    # Step 3 — voice
    click.echo("")
    voice = click.prompt(
        "Step 3/7  voice (口吻)",
        type=click.Choice(
            ["first_party_brand", "observer", "personal", "skip"],
            case_sensitive=False,
        ),
        default="skip",
        show_default=True,
    )
    if voice and voice.lower() != "skip":
        pub["voice"] = voice.lower()

    # Step 4 — perspectives (loop)
    click.echo("")
    click.echo("Step 4/7  signature angles (文章视角)。每行一条，空回车结束。")
    perspectives: list[str] = []
    idx = 1
    while True:
        item = click.prompt(
            f"  perspective #{idx}",
            default="",
            show_default=False,
        ).strip()
        if not item:
            break
        perspectives.append(item)
        idx += 1
    if perspectives:
        pub["perspectives"] = perspectives

    # Step 5 — platform_handles (medium / twitter / ghost)
    click.echo("")
    click.echo("Step 5/7  platform_handles. Each subfield can be skipped.")
    handles: dict[str, dict[str, Any]] = {}

    medium_handle = click.prompt(
        "  medium handle (e.g. @xxx)", default="", show_default=False
    ).strip()
    medium_url = click.prompt(
        "  medium url (e.g. https://medium.com/@xxx)",
        default="",
        show_default=False,
    ).strip()
    medium_block: dict[str, Any] = {}
    if medium_handle:
        medium_block["handle"] = medium_handle
    if medium_url:
        medium_block["url"] = medium_url
    if medium_block:
        handles["medium"] = medium_block

    twitter_handle = click.prompt(
        "  twitter handle (e.g. @yyy)", default="", show_default=False
    ).strip()
    twitter_url = click.prompt(
        "  twitter url", default="", show_default=False
    ).strip()
    twitter_block: dict[str, Any] = {}
    if twitter_handle:
        twitter_block["handle"] = twitter_handle
    if twitter_url:
        twitter_block["url"] = twitter_url
    if twitter_block:
        handles["twitter"] = twitter_block

    ghost_site = click.prompt(
        "  ghost site_url (e.g. https://blog.example.com)",
        default="",
        show_default=False,
    ).strip()
    if ghost_site:
        handles["ghost"] = {"site_url": ghost_site}

    if handles:
        pub["platform_handles"] = handles

    # Step 6 — brand_overlay (logo_path + anchor only; rest inherits global)
    click.echo("")
    click.echo("Step 6/7  per-profile brand_overlay. Other params inherit global preferences.")
    logo_path = click.prompt(
        "  brand_overlay.logo_path", default="", show_default=False
    ).strip()
    anchor = click.prompt(
        "  brand_overlay.anchor (e.g. bottom-right)",
        default="",
        show_default=False,
    ).strip()
    overlay: dict[str, Any] = {}
    if logo_path:
        overlay["logo_path"] = logo_path
    if anchor:
        overlay["anchor"] = anchor
    if overlay:
        pub["brand_overlay"] = overlay

    # Step 7 — canonical_domain
    click.echo("")
    canonical = click.prompt(
        "Step 7/7  canonical_domain (e.g. https://blog.example.com)",
        default="",
        show_default=False,
    ).strip()
    if canonical:
        pub["canonical_domain"] = canonical

    if pub:
        patch["publisher_account"] = pub

    return patch


def _emit_profile_update_event(
    *,
    profile_id: str,
    mode: str,
    source: str,
    path: str | None,
) -> None:
    emit_agent_event(
        source="agentflow.cli.topic_profile",
        event_type="profile.updated",
        payload={
            "profile_id": profile_id,
            "mode": mode,
            "source": source,
            "path": path,
        },
    )


@cli.group("topic-profile")
def topic_profile_group() -> None:
    """Manage user topic profile constraints."""


@topic_profile_group.command("show")
@click.option("--profile", "profile_id", required=True)
@click.option("--json", "as_json", is_flag=True, default=False)
def topic_profile_show(profile_id: str, as_json: bool) -> None:
    state = user_profile_bootstrap_state(profile_id)
    payload = {
        "profile_id": profile_id,
        **state,
    }
    if as_json:
        _emit_json(payload)
        return
    click.echo(json.dumps(payload, ensure_ascii=False, indent=2))


@topic_profile_group.command("init")
@click.option("--profile", "profile_id", required=True)
@click.option("--from-file", "from_file", type=click.Path(exists=True, dir_okay=False))
@click.option("--from-session", "from_session", type=str, default=None)
@click.option(
    "-i",
    "--interactive",
    "interactive",
    is_flag=True,
    default=False,
    help="Run interactive 7-step wizard to assemble the profile patch.",
)
@click.option("--json", "as_json", is_flag=True, default=False)
def topic_profile_init(
    profile_id: str,
    from_file: str | None,
    from_session: str | None,
    interactive: bool,
    as_json: bool,
) -> None:
    if interactive and (from_file or from_session):
        raise click.UsageError(
            "--interactive is mutually exclusive with --from-file / --from-session"
        )

    materialize_user_topic_profiles()

    if interactive:
        patch = _interactive_collect_patch(profile_id)
        if not patch:
            raise click.ClickException(
                "interactive wizard collected no fields; aborting (nothing to write)"
            )
        source = "interactive"
    else:
        patch, source = _resolve_patch(
            profile_id=profile_id,
            from_file=from_file,
            from_session=from_session,
        )

    result = upsert_profile(
        profile_id,
        patch,
        replace_lists=True,
        source=f"topic_profile.init:{source}",
    )
    append_memory_event(
        "topic_profile_updated",
        payload={"profile_id": profile_id, "mode": "init", "source": source},
    )
    _emit_profile_update_event(
        profile_id=profile_id,
        mode="init",
        source=source,
        path=result.get("path"),
    )
    if as_json:
        _emit_json(result)
        return
    click.echo(json.dumps(result, ensure_ascii=False, indent=2))


@topic_profile_group.command("update")
@click.option("--profile", "profile_id", required=True)
@click.option("--from-file", "from_file", type=click.Path(exists=True, dir_okay=False))
@click.option("--from-session", "from_session", type=str, default=None)
@click.option("--json", "as_json", is_flag=True, default=False)
def topic_profile_update(
    profile_id: str,
    from_file: str | None,
    from_session: str | None,
    as_json: bool,
) -> None:
    patch, source = _resolve_patch(
        profile_id=profile_id,
        from_file=from_file,
        from_session=from_session,
    )
    result = upsert_profile(
        profile_id,
        patch,
        replace_lists=True,
        source=f"topic_profile.update:{source}",
    )
    append_memory_event(
        "topic_profile_updated",
        payload={"profile_id": profile_id, "mode": "update", "source": source},
    )
    _emit_profile_update_event(
        profile_id=profile_id,
        mode="update",
        source=source,
        path=result.get("path"),
    )
    if as_json:
        _emit_json(result)
        return
    click.echo(json.dumps(result, ensure_ascii=False, indent=2))


@topic_profile_group.command("suggest")
@click.option("--profile", "profile_id", required=True)
@click.option("--stage", required=True)
@click.option("--title", required=True)
@click.option("--summary", required=True)
@click.option("--from-file", "from_file", type=click.Path(exists=True, dir_okay=False))
@click.option("--risk-level", default="low", show_default=True)
@click.option("--json", "as_json", is_flag=True, default=False)
def topic_profile_suggest(
    profile_id: str,
    stage: str,
    title: str,
    summary: str,
    from_file: str | None,
    risk_level: str,
    as_json: bool,
) -> None:
    payload = _load_patch_from_file(from_file) if from_file else {}
    suggestion = make_learning_suggestion(
        profile_id=profile_id,
        stage=stage,
        title=title,
        summary=summary,
        proposed_patch=payload.get("proposed_patch") if isinstance(payload, dict) else {},
        evidence=payload.get("evidence") if isinstance(payload, dict) else [],
        risk_level=risk_level,
    )
    append_memory_event(
        "topic_profile_suggestion_created",
        payload={"profile_id": profile_id, "suggestion_id": suggestion["id"], "stage": stage},
    )
    if as_json:
        _emit_json(suggestion)
        return
    click.echo(json.dumps(suggestion, ensure_ascii=False, indent=2))


@topic_profile_group.command("review")
@click.argument("suggestion_id")
@click.option("--json", "as_json", is_flag=True, default=False)
def topic_profile_review(suggestion_id: str, as_json: bool) -> None:
    payload = review_suggestion(suggestion_id)
    if as_json:
        _emit_json(payload)
        return
    click.echo(json.dumps(payload, ensure_ascii=False, indent=2))


@topic_profile_group.command("apply")
@click.argument("suggestion_id")
@click.option("--json", "as_json", is_flag=True, default=False)
def topic_profile_apply(suggestion_id: str, as_json: bool) -> None:
    payload = apply_suggestion(suggestion_id)
    append_memory_event(
        "topic_profile_suggestion_applied",
        payload={
            "profile_id": payload["suggestion"].get("profile_id"),
            "suggestion_id": suggestion_id,
        },
    )
    emit_agent_event(
        source="agentflow.cli.topic_profile",
        event_type="profile.suggestion_applied",
        payload={
            "profile_id": payload["suggestion"].get("profile_id"),
            "mode": "apply",
            "source": payload["applied"].get("source"),
            "suggestion_id": suggestion_id,
            "path": payload["applied"].get("path"),
        },
    )
    if as_json:
        _emit_json(payload)
        return
    click.echo(json.dumps(payload, ensure_ascii=False, indent=2))


@topic_profile_group.command("suggestion-show")
@click.argument("suggestion_id")
@click.option("--json", "as_json", is_flag=True, default=False)
def topic_profile_suggestion_show(suggestion_id: str, as_json: bool) -> None:
    payload = load_suggestion(suggestion_id)
    if as_json:
        _emit_json(payload)
        return
    click.echo(json.dumps(payload, ensure_ascii=False, indent=2))


@topic_profile_group.command("suggestion-list")
@click.option("--profile", "profile_id", default=None)
@click.option("--status", "status", default="pending", show_default=True)
@click.option("--json", "as_json", is_flag=True, default=False)
def topic_profile_suggestion_list(
    profile_id: str | None,
    status: str | None,
    as_json: bool,
) -> None:
    items = list_suggestions(profile_id=profile_id, status=status or None)
    payload = {
        "count": len(items),
        "items": items,
    }
    if as_json:
        _emit_json(payload)
        return
    click.echo(json.dumps(payload, ensure_ascii=False, indent=2))


@topic_profile_group.command("materialize")
@click.option("--json", "as_json", is_flag=True, default=False)
def topic_profile_materialize(as_json: bool) -> None:
    payload, path = materialize_user_topic_profiles()
    out = {"path": str(path), "profiles": sorted((load_user_topic_profiles().get("profiles") or {}).keys())}
    if as_json:
        _emit_json(out)
        return
    click.echo(json.dumps(out, ensure_ascii=False, indent=2))


# ---- derive ---------------------------------------------------------------

_DERIVE_SYSTEM_PROMPT = (
    "你是一名内容运营顾问。任务：从用户给的一段 publisher 自我描述里反推出"
    "若干条独立的 profile 候选条目（每一条都是一条独立 suggestion）。"
    "覆盖 5 类 stage：keyword_groups / do / dont / perspectives / product_facts。"
    "严格按照 JSON 输出，不要 markdown 围栏，不要解释。"
)

_DERIVE_USER_PROMPT_TMPL = """\
publisher 自我描述（default_description）：
\"\"\"
{description}
\"\"\"

请输出 JSON，schema 如下：
{{
  "suggestions": [
    {{
      "stage": "keyword_groups" | "do" | "dont" | "perspectives" | "product_facts",
      "title": "≤ 40 字的简短标题",
      "summary": "≤ 200 字的解释，写给人看",
      "patch": <stage 对应的 patch 片段；见下方>
    }}
  ]
}}

各 stage 的 patch 形状：
- keyword_groups: {{"keyword_groups": {{"<group_name>": ["term1", "term2"]}}}}
  group_name 例如 core / transport / industry / 等等，按内容自由分类。
- do:             {{"publisher_account": {{"do":   ["rule"]}}}}
- dont:           {{"publisher_account": {{"dont": ["rule"]}}}}
- perspectives:   {{"publisher_account": {{"perspectives": ["angle"]}}}}
- product_facts:  {{"publisher_account": {{"product_facts": ["fact"]}}}}

要求：
- 每条 suggestion 只塞 1~3 个具体条目，让 reviewer 能逐条 apply/dismiss。
- 不要重复，不要凑数；想不出某 stage 就跳过它。
- 总 suggestion 条数上限 {max_suggestions} 条。
"""


def _run_async(coro):
    """Run an async coroutine from a sync click command."""
    return asyncio.run(coro)


def _derive_call_llm(description: str, max_suggestions: int) -> dict[str, Any]:
    """Call LLMClient.chat_json with the derive prompt. Returns parsed dict.

    Raises a ``click.ClickException`` (graceful, non-zero exit) on any failure
    instead of propagating raw exceptions.
    """
    try:
        from agentflow.shared.llm_client import LLMClient
    except Exception as exc:  # pragma: no cover - import-time failure
        raise click.ClickException(
            f"failed to import LLMClient: {type(exc).__name__}: {exc}"
        ) from exc

    prompt = _DERIVE_USER_PROMPT_TMPL.format(
        description=description.strip(),
        max_suggestions=max_suggestions,
    )

    try:
        client = LLMClient()
        raw = _run_async(
            client.chat_json(
                prompt_family="topic_profile_derive",
                prompt=prompt,
                system=_DERIVE_SYSTEM_PROMPT,
                max_tokens=2000,
            )
        )
    except Exception as exc:
        raise click.ClickException(
            f"LLM call failed: {type(exc).__name__}: {exc}"
        ) from exc

    if not isinstance(raw, dict):
        raise click.ClickException(
            f"LLM returned non-object payload (type={type(raw).__name__})"
        )
    return raw


_VALID_DERIVE_STAGES = {
    "keyword_groups",
    "do",
    "dont",
    "perspectives",
    "product_facts",
}


def _normalize_derive_suggestions(
    raw: dict[str, Any],
    *,
    max_suggestions: int,
) -> list[dict[str, Any]]:
    items_raw = raw.get("suggestions")
    if not isinstance(items_raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in items_raw:
        if not isinstance(item, dict):
            continue
        stage = str(item.get("stage") or "").strip()
        if stage not in _VALID_DERIVE_STAGES:
            continue
        title = str(item.get("title") or "").strip()
        summary = str(item.get("summary") or "").strip()
        patch = item.get("patch")
        if not isinstance(patch, dict) or not patch:
            continue
        if not title:
            title = f"derived {stage} suggestion"
        if not summary:
            summary = "(no summary provided by LLM)"
        out.append(
            {
                "stage": stage,
                "title": title,
                "summary": summary,
                "patch": patch,
            }
        )
        if len(out) >= max_suggestions:
            break
    return out


@topic_profile_group.command("derive")
@click.option("--profile", "profile_id", required=True)
@click.option(
    "--max-suggestions",
    type=int,
    default=10,
    show_default=True,
    help="Upper bound on number of suggestions the LLM may emit.",
)
@click.option("--json", "as_json", is_flag=True, default=False)
def topic_profile_derive(
    profile_id: str,
    max_suggestions: int,
    as_json: bool,
) -> None:
    """从 profile.publisher_account.default_description 让 LLM 反推 keyword_groups /
    do / dont / perspectives / product_facts，每条作为独立 suggestion 入库，
    不直接覆盖 profile（用户后续走 review/apply/dismiss）。"""

    if max_suggestions <= 0:
        raise click.UsageError("--max-suggestions must be > 0")

    try:
        profile = get_topic_profile(profile_id)
    except Exception as exc:
        raise click.ClickException(
            f"could not load profile {profile_id!r}: {type(exc).__name__}: {exc}"
        ) from exc

    description = topic_profile_default_description(profile)
    if not description:
        click.echo(
            f"profile {profile_id!r} has no publisher_account.default_description; "
            "nothing to derive from. Run `af topic-profile init -i` (Step 2) first.",
            err=True,
        )
        sys.exit(1)

    raw = _derive_call_llm(description, max_suggestions)
    suggestions_norm = _normalize_derive_suggestions(
        raw, max_suggestions=max_suggestions
    )

    if not suggestions_norm:
        click.echo(
            "LLM returned no usable suggestions (after normalization).",
            err=True,
        )
        sys.exit(1)

    created: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for item in suggestions_norm:
        try:
            saved = make_learning_suggestion(
                profile_id=profile_id,
                stage=item["stage"],
                title=item["title"],
                summary=item["summary"],
                proposed_patch=item["patch"],
                evidence=[
                    {
                        "type": "derive",
                        "source": "topic_profile.derive",
                        "default_description": description,
                    }
                ],
                risk_level="low",
            )
        except Exception as exc:
            failures.append(
                {
                    "stage": item["stage"],
                    "title": item["title"],
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        append_memory_event(
            "topic_profile_suggestion_created",
            payload={
                "profile_id": profile_id,
                "suggestion_id": saved["id"],
                "stage": item["stage"],
                "source": "derive",
            },
        )
        created.append(saved)

    payload = {
        "profile_id": profile_id,
        "created_count": len(created),
        "failure_count": len(failures),
        "created": created,
        "failures": failures,
    }

    if as_json:
        _emit_json(payload)
    else:
        click.echo(json.dumps(payload, ensure_ascii=False, indent=2))

    if not created:
        sys.exit(1)


# ---------------------------------------------------------------------------
# af topic-profile list  (v1.0.4) — markdown table or JSON list
# af topic-profile set-active <id> (v1.0.4) — switch active profile
# ---------------------------------------------------------------------------


def _intents_current_path() -> Path:
    """Return ``~/.agentflow/intents/current.yaml`` (creating the dir)."""
    ensure_user_dirs()
    p = agentflow_home() / "intents"
    p.mkdir(parents=True, exist_ok=True)
    return p / "current.yaml"


def _read_active_profile_id() -> str | None:
    """Resolve the currently active profile id from ``intents/current.yaml``.

    Returns ``None`` when the intent file is missing, malformed, or has no
    ``profile.id`` set. Tolerates a flat ``profile_id`` as a back-compat
    shortcut.
    """
    path = _intents_current_path()
    if not path.exists():
        return None
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, OSError):
        return None
    if not isinstance(raw, dict):
        return None
    profile = raw.get("profile")
    if isinstance(profile, dict):
        pid = str(profile.get("id") or "").strip()
        if pid:
            return pid
    flat = str(raw.get("profile_id") or "").strip()
    return flat or None


def _last_modified_iso(path: Path) -> str:
    try:
        ts = path.stat().st_mtime
    except OSError:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _profile_brand(profile: Any) -> str:
    if not isinstance(profile, dict):
        return ""
    pub = profile.get("publisher_account")
    if isinstance(pub, dict):
        brand = str(pub.get("brand") or "").strip()
        if brand:
            return brand
    label = str(profile.get("label") or "").strip()
    return label


def _collect_profile_rows() -> list[dict[str, Any]]:
    """Walk the user-managed profile store and return one dict per profile.

    Source-of-truth for v1.0.4 is the per-profile ``~/.agentflow/profiles/<id>.yaml``
    layout when present; legacy installs that still keep everything inside the
    aggregate ``topic_profiles.yaml::profiles`` mapping are also surfaced so
    the operator never sees an empty list mid-migration.
    """
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    active = _read_active_profile_id()

    profiles_dir = agentflow_home() / "profiles"
    if profiles_dir.exists() and profiles_dir.is_dir():
        for entry in sorted(profiles_dir.glob("*.yaml")):
            pid = entry.stem
            if not pid or pid in seen:
                continue
            try:
                payload = yaml.safe_load(entry.read_text(encoding="utf-8")) or {}
            except (yaml.YAMLError, OSError):
                payload = {}
            rows.append({
                "id": pid,
                "brand": _profile_brand(payload),
                "last_modified": _last_modified_iso(entry),
                "is_active": pid == active,
                "path": str(entry),
            })
            seen.add(pid)

    aggregate_path = user_topic_profiles_path()
    if aggregate_path.exists():
        try:
            data = load_user_topic_profiles()
        except Exception:
            data = {}
        profiles = data.get("profiles") if isinstance(data, dict) else None
        if isinstance(profiles, dict):
            mtime_iso = _last_modified_iso(aggregate_path)
            for pid, payload in profiles.items():
                if not pid or pid in seen:
                    continue
                rows.append({
                    "id": str(pid),
                    "brand": _profile_brand(payload),
                    "last_modified": mtime_iso,
                    "is_active": str(pid) == active,
                    "path": str(aggregate_path),
                })
                seen.add(str(pid))

    rows.sort(key=lambda r: r["id"])
    return rows


def _profile_exists(profile_id: str) -> bool:
    """Whether a profile with ``profile_id`` exists in either store."""
    if not profile_id:
        return False
    profiles_dir = agentflow_home() / "profiles"
    if (profiles_dir / f"{profile_id}.yaml").exists():
        return True
    if user_topic_profiles_path().exists():
        try:
            data = load_user_topic_profiles() or {}
        except Exception:
            data = {}
        profiles = data.get("profiles") if isinstance(data, dict) else None
        if isinstance(profiles, dict) and profile_id in profiles:
            return True
    return False


def _render_profiles_markdown(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "(no profiles found)"
    header = "| id | brand | last_modified | is_active |"
    sep = "| --- | --- | --- | --- |"
    lines = [header, sep]
    for row in rows:
        marker = "✓" if row["is_active"] else ""
        brand = row["brand"] or "—"
        lines.append(
            f"| {row['id']} | {brand} | {row['last_modified'] or '—'} | {marker} |"
        )
    return "\n".join(lines)


@topic_profile_group.command("list")
@click.option("--json", "as_json", is_flag=True, default=False)
def topic_profile_list(as_json: bool) -> None:
    """List all profiles (id, brand, last_modified ISO, is_active marker)."""
    rows = _collect_profile_rows()
    if as_json:
        _emit_json({"count": len(rows), "items": rows})
        return
    click.echo(_render_profiles_markdown(rows))


@topic_profile_group.command("set-active")
@click.argument("profile_id")
@click.option("--json", "as_json", is_flag=True, default=False)
def topic_profile_set_active(profile_id: str, as_json: bool) -> None:
    """Switch the active profile by writing ``profile_id`` into
    ``~/.agentflow/intents/current.yaml``.

    Validates that the profile exists in the user store before writing.
    Emits a ``profile_switched`` memory event so ``af report`` can show it.
    """
    profile_id = (profile_id or "").strip()
    if not profile_id:
        raise click.ClickException("profile_id is required")
    if not _profile_exists(profile_id):
        raise click.ClickException(
            f"profile {profile_id!r} not found in user store "
            "(checked ~/.agentflow/profiles/<id>.yaml and "
            "~/.agentflow/topic_profiles.yaml::profiles)"
        )

    prev_id = _read_active_profile_id()
    path = _intents_current_path()
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if isinstance(loaded, dict):
                existing = loaded
        except (yaml.YAMLError, OSError):
            existing = {}

    profile_block = existing.get("profile")
    if not isinstance(profile_block, dict):
        profile_block = {}
    profile_block["id"] = profile_id

    payload: dict[str, Any] = dict(existing)
    payload["profile"] = profile_block
    payload["profile_id"] = profile_id
    payload.setdefault("schema_version", 1)
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()

    path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    append_memory_event(
        "profile_switched",
        payload={"from": prev_id, "to": profile_id},
    )

    result = {
        "ok": True,
        "from": prev_id,
        "to": profile_id,
        "path": str(path),
    }
    if as_json:
        _emit_json(result)
        return
    click.echo(f"active profile: {prev_id or '(none)'} → {profile_id}")
    click.echo(f"  written to {path}")
