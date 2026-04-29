"""`af onboard` — interactive credential setup wizard.

Philosophy
    Mirror the ergonomics of `claude auth login` / `oai-cli auth login`: walk
    a fresh user through every credential agentflow needs, one section at a
    time, and validate each one against its live API before moving on.

    This is an INTERACTIVE wizard. Non-interactive callers (CI, scripts, ops
    runbooks) should use ``af doctor`` for read-only health and edit ``.env``
    directly. ``af onboard --check`` provides a non-interactive status pass
    that shares the section taxonomy used by the wizard.

Self-registering: imported lazily by ``agentflow.cli.commands``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import click

from agentflow.cli.commands import _emit_json, cli


# ---------------------------------------------------------------------------
# Section catalog
# ---------------------------------------------------------------------------


def _S(env: str, prompt: str) -> dict[str, Any]:  # secret key
    return {"env": env, "prompt": prompt, "secret": True}


def _P(env: str, prompt: str) -> dict[str, Any]:  # plain (visible) key
    return {"env": env, "prompt": prompt, "secret": False}


_SECTIONS: list[dict[str, Any]] = [
    # v1.0.6 reorder: identity / writing-engine FIRST, then media credentials.
    # Previous ordering put `telegram` first which surprised operators ("why
    # is onboard asking for media before brand?"). Identity is now front-loaded:
    # profile → llm → embeddings → atlas → telegram (Mode B/C only) → per-platform.
    {"id": "profile", "label": "Profile / identity (brand, voice, sources, rules)",
     "optional": False,
     "why": "AgentFlow writes AS your brand in your voice. Without a profile "
            "every downstream agent leans on a generic template that won't sound "
            "like you. This step doesn't write env keys — it writes your "
            "publisher profile to ~/.agentflow/topic_profiles.yaml and is the "
            "TRUE first step. Run `af topic-profile init -i --profile <id>` "
            "(interactive) or `--from-file <path>` (yaml). Existing profile? "
            "skip this section.",
     "required_for": ["af hotspots", "af write", "af fill"],
     "probe": None,
     "keys": [],
     "interactive_action": "topic_profile_init"},
    {"id": "llm", "label": "LLM provider (Moonshot / Anthropic)", "optional": False,
     "why": "Powers D1/D2/D3 generation. Needs at least one working provider.",
     "required_for": ["af hotspots", "af write", "af fill", "af edit"],
     "probe": "check_moonshot", "probe_fallback": "check_anthropic",
     "keys": [_S("MOONSHOT_API_KEY", "Moonshot Kimi API key (primary)"),
              _S("ANTHROPIC_API_KEY", "Anthropic Claude API key (optional fallback)")]},
    {"id": "embeddings", "label": "Embedding provider (Jina / OpenAI)", "optional": False,
     "why": "DBSCAN clustering for the hotspots scan.",
     "required_for": ["af hotspots"],
     "probe": "check_jina", "probe_fallback": "check_openai",
     "keys": [_S("JINA_API_KEY", "Jina API key (primary; 10M tokens free)"),
              _S("OPENAI_API_KEY", "OpenAI API key (optional fallback for embeddings)")]},
    {"id": "atlas", "label": "AtlasCloud (image generation)", "optional": False,
     "why": "GPT Image 2 relay for cover + inline image generation. "
            "For brand-specific visuals, set "
            "publisher_account.image_prompt_hints in topic_profiles.yaml.",
     "required_for": ["af image-generate", "image gate"], "probe": "check_atlas",
     "keys": [_S("ATLASCLOUD_API_KEY", "AtlasCloud API key")]},
    {"id": "telegram", "label": "Telegram review bot (Mode B/C only)",
     "optional": True,
     "why": "Mode B/C only — phone-based approve/reject for Gate A/B/C cards. "
            "Mode A (harness-only via Claude Code / Cursor) does NOT need this. "
            "Skip this section if the operator works inside the chat session.",
     "required_for": ["af review-daemon (Mode B/C)"], "probe": "check_telegram",
     "keys": [_S("TELEGRAM_BOT_TOKEN", "Bot token from @BotFather"),
              _P("TELEGRAM_REVIEW_CHAT_ID",
                 "Your chat_id (or blank — daemon auto-captures on /start)")]},
    {"id": "twitter", "label": "Twitter / X (OAuth 1.0a + bearer)", "optional": True,
     "why": "Posts threads / single tweets and reads KOL signals during hotspots.",
     "required_for": ["af tweet-*", "--platforms twitter_thread/single"],
     "probe": "check_twitter",
     "keys": [_S("TWITTER_BEARER_TOKEN", "Bearer token (read-only, used by hotspots)"),
              _S("TWITTER_CONSUMER_KEY", "Consumer key (OAuth 1.0a)"),
              _S("TWITTER_CONSUMER_SECRET", "Consumer secret"),
              _S("TWITTER_USER_ACCESS_TOKEN", "User access token"),
              _S("TWITTER_USER_ACCESS_SECRET", "User access secret"),
              _P("TWITTER_HANDLE", "Your Twitter handle (e.g. @you)")]},
    {"id": "ghost", "label": "Ghost CMS (Admin API)", "optional": True,
     "why": "Primary blog target in v0.1.",
     "required_for": ["--platforms ghost"], "probe": "check_ghost",
     "keys": [_P("GHOST_ADMIN_API_URL", "Ghost site URL (e.g. https://yoursite.ghost.io)"),
              _S("GHOST_ADMIN_API_KEY", "Admin API key (24hex:hex format)")]},
    {"id": "linkedin", "label": "LinkedIn (OAuth 2.0)", "optional": True,
     "why": "Cross-post articles as LinkedIn posts.",
     "required_for": ["--platforms linkedin"], "probe": "check_linkedin",
     "keys": [_S("LINKEDIN_ACCESS_TOKEN", "OAuth 2.0 access token"),
              _P("LINKEDIN_PERSON_URN", "Person URN (urn:li:person:...)")]},
    {"id": "webhook", "label": "Webhook publisher (custom CMS / relay)", "optional": True,
     "why": "POSTs the finished article to any HTTP receiver you control.",
     "required_for": ["--platforms webhook"], "probe": None,
     "keys": [_P("WEBHOOK_PUBLISH_URL", "Receiver URL"),
              _S("WEBHOOK_AUTH_HEADER", "Authorization value (e.g. 'Bearer xxx' or 'X-API-Key:xxx')"),
              _P("WEBHOOK_FORMAT", "Wire format (json|multipart)")]},
    {"id": "resend", "label": "Resend newsletter", "optional": True,
     "why": "Email fanout to your subscriber audience.",
     "required_for": ["af newsletter-*"], "probe": None,
     "keys": [_S("RESEND_API_KEY", "Resend API key"),
              _P("NEWSLETTER_FROM_EMAIL", "From: address (must be on a verified domain)"),
              _P("NEWSLETTER_FROM_NAME", "From: display name"),
              _P("NEWSLETTER_REPLY_TO", "Reply-To: address"),
              _P("NEWSLETTER_AUDIENCE_ID", "Audience id (Resend dashboard -> Audiences)")]},
    # Sample-driven, not env-driven. Walked through af learn-from-handle.
    {"id": "style", "label": "Authoring style (samples → voice)", "optional": True,
     "why": "Seed the style profile from your past posts so D2 fill matches "
            "your voice. Without this, D2 leans on the generic style template.",
     "required_for": ["af write", "af fill"], "probe": None,
     "keys": [],
     "interactive_action": "learn_from_handle"},
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _env_path() -> Path:
    """Return the canonical secret-file path the wizard should write to.

    v1.0.4 moved keys out of the runtime repo and into ``~/.agentflow/secrets/.env``
    (the user's "key folder"). Onboard creates the dir at mode 0700 and the file
    at mode 0600 on first write so subsequent reads are still operator-private.

    The legacy ``backend/.env`` location is still RESOLVED at load time
    (see ``commands._candidate_secret_files``) for back-compat with installs
    predating v1.0.4 — but ``af onboard`` no longer writes there. Operators on
    older installs who run ``af onboard`` will have their keys quietly migrated
    to the new location; the legacy file becomes a stale fallback.
    """
    secrets_dir = Path.home() / ".agentflow" / "secrets"
    if not secrets_dir.exists():
        secrets_dir.mkdir(parents=True, exist_ok=True)
        try:
            secrets_dir.chmod(0o700)
        except OSError:
            pass
    target = secrets_dir / ".env"
    return target


def _ensure_file_perms(path: Path) -> None:
    """chmod 0600 on first creation so a multi-user host can't snoop secrets."""
    if path.exists():
        try:
            path.chmod(0o600)
        except OSError:
            pass


def _read_env_file(path: Path) -> dict[str, str]:
    try:
        from dotenv import dotenv_values
    except ImportError as err:  # pragma: no cover
        raise click.ClickException(f"python-dotenv not installed: {err}")
    if not path.exists():
        return {}
    return {k: (v or "") for k, v in dotenv_values(path).items()}


def _set_env_value(path: Path, key: str, value: str) -> None:
    from dotenv import set_key

    if not path.exists():
        path.touch()
        _ensure_file_perms(path)
    set_key(str(path), key, value, quote_mode="never")
    _ensure_file_perms(path)
    os.environ[key] = value


def _mask(value: str, *, secret: bool) -> str:
    if not value:
        return "(unset)"
    if not secret:
        return value
    if len(value) <= 6:
        return "*" * len(value)
    return f"{value[:4]}{'*' * max(1, len(value) - 6)}{value[-2:]}"


def _section_by_id(sid: str) -> dict[str, Any] | None:
    return next((s for s in _SECTIONS if s["id"] == sid), None)


def _run_probe(name: str | None, *, fresh: bool = False):
    if not name:
        return None
    from agentflow.agent_review import preflight as _pf

    fn = getattr(_pf, name, None)
    if fn is None:
        return None
    try:
        try:
            return fn(fresh=fresh)
        except TypeError:
            return fn()
    except Exception as err:  # pragma: no cover
        click.echo(f"  probe error: {err}")
        return None


def _style_corpus_status() -> tuple[str, str]:
    """Sample-driven status: count style_corpus entries to gauge seeding."""
    from agentflow.shared.bootstrap import agentflow_home

    corpus = agentflow_home() / "style_corpus"
    if not corpus.exists():
        return "unset", "no style_corpus dir — run af learn-from-handle"
    json_samples = [p for p in corpus.iterdir() if p.is_file() and p.suffix == ".json"]
    if not json_samples:
        return "unset", "style_corpus is empty — run af learn-from-handle"
    return "ok", f"{len(json_samples)} sample(s) ingested"


def _section_status(section: dict[str, Any]) -> dict[str, Any]:
    """Compute status used by both --check and the wizard summary."""
    # Sample-driven sections (no env keys; status comes from disk artifacts).
    if section.get("interactive_action") == "learn_from_handle":
        status, message = _style_corpus_status()
        return {
            "section": section["id"],
            "label": section["label"],
            "status": status,
            "required": not section.get("optional", False),
            "message": message,
        }

    env = _read_env_file(_env_path())
    keys = section["keys"]
    set_keys = [k["env"] for k in keys if env.get(k["env"], "").strip()]

    primary = _run_probe(section.get("probe"))
    fallback = _run_probe(section.get("probe_fallback"))
    candidates = [c for c in (primary, fallback) if c is not None]

    if not set_keys and not any(getattr(c, "present", False) for c in candidates):
        status, message = "unset", f"no keys set ({', '.join(k['env'] for k in keys)})"
    elif candidates and any(getattr(c, "ok", False) for c in candidates):
        ok = next(c for c in candidates if getattr(c, "ok", False))
        status, message = "ok", ok.message or "valid"
    elif candidates and any(getattr(c, "valid", None) is False for c in candidates):
        bad = next(c for c in candidates if getattr(c, "valid", None) is False)
        status, message = "invalid", bad.message or "invalid"
    elif set_keys:
        status = "ok" if section.get("probe") is None else "missing"
        message = "set" if status == "ok" else "set but unverified"
    else:
        status, message = "missing", "incomplete"

    return {
        "section": section["id"],
        "label": section["label"],
        "status": status,
        "required": not section.get("optional", False),
        "message": message,
    }


# ---------------------------------------------------------------------------
# Wizard
# ---------------------------------------------------------------------------


def _walk_style_section(section: dict[str, Any]) -> bool:
    """Sample-driven section: ingest from a handle / URL via learn-from-handle.

    Skips silently when the user pastes nothing — the section is optional and
    skipping is a valid outcome (D2 just falls back to the generic template).
    """
    click.echo()
    click.echo(f"=== {section['label']} ===")
    click.echo(f"  why:        {section['why']}")
    click.echo(f"  needed for: {', '.join(section['required_for'])}")
    click.echo(f"  optional:   yes")
    click.echo()
    cur_status, cur_msg = _style_corpus_status()
    click.echo(f"  current corpus: {cur_msg}")
    click.echo()
    try:
        handle = click.prompt(
            "  Paste a handle or URL to ingest (medium.com/@user · "
            "alice.substack.com · 0xabc.mirror.xyz · RSS feed) "
            "[enter=skip]",
            default="", show_default=False,
        )
    except click.Abort:
        return False
    handle = (handle or "").strip()
    if not handle:
        click.echo("  (no handle provided — D2 will use the generic style template)")
        return True

    # Dispatch through the public learn-from-handle path so prompts +
    # ingestion stay consistent with the standalone CLI.
    try:
        max_samples = click.prompt(
            "  How many recent posts to ingest?", default=5, type=int,
            show_default=True,
        )
    except click.Abort:
        return False
    max_samples = max(1, min(int(max_samples or 5), 20))

    from agentflow.agent_d0.handle_fetcher import resolve_handle_to_urls
    from agentflow.agent_d0.main import run as _d0_run

    try:
        urls, label = resolve_handle_to_urls(handle, max_samples=max_samples)
    except Exception as err:
        click.echo(f"  ✗ resolver failed: {err}")
        return True
    if not urls:
        click.echo(f"  ✗ no URLs found for {handle!r} ({label}); try the RSS feed URL directly")
        return True
    click.echo(f"  resolved {label} → {len(urls)} URL(s):")
    for u in urls:
        click.echo(f"    - {u}")
    if not click.confirm("  ingest these now?", default=True):
        return True
    try:
        _d0_run(url=urls)
    except Exception as err:
        click.echo(f"  ✗ D0 ingestion failed: {err}")
        return True

    desc = click.prompt(
        "  Optional: a one-line description of your voice [enter=skip]",
        default="", show_default=False,
    ).strip()
    if desc:
        from agentflow.shared.bootstrap import agentflow_home
        desc_path = agentflow_home() / "style_corpus" / "manual_description.md"
        desc_path.parent.mkdir(parents=True, exist_ok=True)
        existing = desc_path.read_text(encoding="utf-8") if desc_path.exists() else ""
        desc_path.write_text(
            existing + f"\n## from onboard ({label})\n\n{desc}\n",
            encoding="utf-8",
        )
        click.echo(f"  → saved description to {desc_path}")

    extra = click.prompt(
        "  Paste an additional sample article URL [enter=skip]",
        default="", show_default=False,
    ).strip()
    if extra:
        try:
            _d0_run(url=[extra])
            click.echo(f"  → ingested extra sample: {extra}")
        except Exception as err:
            click.echo(f"  ✗ extra sample failed: {err}")

    new_status, new_msg = _style_corpus_status()
    click.echo(f"  ✓ corpus now: {new_msg}")
    return True


def _walk_section(section: dict[str, Any], env_path: Path) -> bool:
    """Walk one section. Returns True to continue, False to abort wizard."""
    if section.get("interactive_action") == "learn_from_handle":
        return _walk_style_section(section)

    click.echo()
    click.echo(f"=== {section['label']} ===")
    click.echo(f"  why:        {section['why']}")
    click.echo(f"  needed for: {', '.join(section['required_for'])}")
    click.echo(f"  optional:   {'yes' if section.get('optional') else 'no'}")
    click.echo()

    env = _read_env_file(env_path)
    skipped = False
    for key in section["keys"]:
        env_var = key["env"]
        secret = bool(key.get("secret"))
        current = env.get(env_var, "")
        click.echo(f"  current {env_var}: "
                   f"{_mask(current, secret=secret) if current else '(unset)'}")
        try:
            entered = click.prompt(
                f"  {key['prompt']} [enter=keep, s=skip section]",
                default="", show_default=False, hide_input=secret,
            )
        except click.Abort:
            return False
        entered = (entered or "").strip()
        if entered.lower() == "s":
            skipped = True
            break
        if not entered:
            continue
        _set_env_value(env_path, env_var, entered)
        env[env_var] = entered
        click.echo(f"  saved {env_var}.")

    if skipped:
        click.echo("  (section skipped)")
        return True

    primary = _run_probe(section.get("probe"), fresh=True)
    fallback = _run_probe(section.get("probe_fallback"), fresh=True)
    candidates = [c for c in (primary, fallback) if c is not None]
    if not candidates:
        click.echo("  (no remote probe — trust the env values)")
        return True
    if any(getattr(c, "ok", False) for c in candidates):
        good = next(c for c in candidates if getattr(c, "ok", False))
        click.echo(f"  ✓ valid — {good.message}")
        return True
    msgs = "; ".join(f"{c.name}: {c.message}" for c in candidates)
    click.echo(f"  ✗ {msgs}")
    if section.get("optional"):
        return True
    return click.confirm("  continue anyway?", default=False)


def _print_check_line(status: dict[str, Any]) -> None:
    sid = status["section"]
    msg = status["message"]
    if status["status"] == "ok":
        click.echo(f"  [ ✓ {sid:<11} ] {msg}")
        return
    if status["status"] in ("missing", "invalid"):
        kind = "required" if status["required"] else "optional"
        click.echo(f"  [ ✗ {sid:<11} ] {kind}, {status['status']}: {msg}")
        return
    kind = "required" if status["required"] else "optional"
    click.echo(f"  [ - {sid:<11} ] {kind}, unset: {msg}")


def _check_mode(as_json: bool) -> int:
    statuses = [_section_status(s) for s in _SECTIONS]
    if as_json:
        _emit_json(statuses)
    else:
        click.echo(f"onboard status (env: {_env_path()})")
        click.echo("-" * 60)
        for st in statuses:
            _print_check_line(st)
    broken = [s for s in statuses if s["required"] and s["status"] != "ok"]
    return 1 if broken else 0


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------


@cli.command(
    "onboard",
    help="Interactive setup wizard for .env credentials. "
    "Use --check for non-interactive status; --section <id> to redo one part.",
)
@click.option(
    "--section", "section_id", default=None,
    help=f"Run a single section. One of: {', '.join(s['id'] for s in _SECTIONS)}",
)
@click.option(
    "--check", "check_only", is_flag=True, default=False,
    help="Non-interactive status. Exits 0 if every required section validates.",
)
@click.option(
    "--json", "as_json", is_flag=True, default=False,
    help="Machine-readable output (only meaningful with --check).",
)
def onboard_cmd(section_id: str | None, check_only: bool, as_json: bool) -> None:
    if check_only:
        rc = _check_mode(as_json)
        if rc:
            raise click.exceptions.Exit(rc)
        return

    if as_json:
        raise click.UsageError("--json only supported with --check")

    env_path = _env_path()
    click.echo("agentflow onboard wizard")
    click.echo(f"  .env target: {env_path} "
               f"({'present' if env_path.exists() else 'will be created'})")

    if section_id:
        sec = _section_by_id(section_id)
        if not sec:
            raise click.UsageError(
                f"unknown section {section_id!r}. "
                f"Choose from: {', '.join(s['id'] for s in _SECTIONS)}"
            )
        _walk_section(sec, env_path)
    else:
        for sec in _SECTIONS:
            if not _walk_section(sec, env_path):
                click.echo("\naborted.")
                return

    click.echo()
    click.echo("=" * 60)
    click.echo("Final status")
    click.echo("=" * 60)
    statuses = [_section_status(s) for s in _SECTIONS]
    for st in statuses:
        _print_check_line(st)
    ready = [s["section"] for s in statuses if s["status"] == "ok"]
    click.echo()
    click.echo(f"ready for: {', '.join(ready) if ready else '(nothing yet)'}")
