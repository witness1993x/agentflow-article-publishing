"""`af bootstrap` — one-shot first-run setup.

Condenses the multi-step install ritual (venv check, .env from template, skill
harness install, onboard pointer, profile/style hints) into a single command.

Each step runs independently and reports its own status — a failure in one
step does NOT abort the rest. This matches the "graceful guidance" tone of
the README walkthrough rather than the strict gate behavior of `af doctor`.

New flags (--mock / --start-daemon / --first-run) extend bootstrap into a
fully automated first-run path:

    af bootstrap --mock --first-run --start-daemon
        → set MOCK_LLM=true, prompt TG creds, install skills, start daemon,
          verify heartbeat, kick a hotspots run.
    af bootstrap --first-run --start-daemon
        → run interactive `af onboard`, install skills, start daemon,
          kick hotspots.
    af bootstrap --start-daemon
        → assume already-onboarded; just spawn daemon + verify heartbeat.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import click

from agentflow.cli.commands import _emit_json, cli


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    """Locate the repo root (the directory containing `backend/`).

    Mirrors `skill_commands._repo_root` — we duplicate to keep this module's
    import surface tight and avoid coupling to skill_commands' internals.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if parent.name == "backend":
            return parent.parent
    return Path(__file__).resolve().parents[3]


def _af_argv(*args: str) -> list[str]:
    """Build an argv that invokes ``af`` from a subprocess.

    Local copy of ``agent_review.triggers._af_argv`` — we can't import that
    here without risking a circular import (triggers pulls in the cli during
    its own setup paths in some configurations).
    """
    af_script = Path(sys.executable).parent / "af"
    if af_script.exists():
        return [str(af_script), *args]
    return [
        sys.executable,
        "-c",
        "import sys; from agentflow.cli.commands import cli; cli(args=sys.argv[1:])",
        *args,
    ]


def _step(name: str) -> dict[str, Any]:
    return {"step": name, "status": "pending", "detail": None}


def _read_env_text(env_path: Path) -> str:
    """Read .env content; tolerate missing files."""
    try:
        return env_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _write_env_text(env_path: Path, text: str) -> None:
    env_path.write_text(text, encoding="utf-8")


def _upsert_env_var(text: str, key: str, value: str) -> str:
    """Replace `KEY=...` line if present, else append `KEY=value` at EOF.

    Preserves line ordering for replacements. Appends a trailing newline if
    the file is non-empty and lacks one.
    """
    pattern = re.compile(rf"^{re.escape(key)}\s*=.*$", re.MULTILINE)
    if pattern.search(text):
        return pattern.sub(f"{key}={value}", text)
    if text and not text.endswith("\n"):
        text += "\n"
    return text + f"{key}={value}\n"


def _env_var_value(text: str, key: str) -> str | None:
    """Return the value of KEY in .env text, or None if absent.

    Strips surrounding quotes and whitespace. Returns "" for `KEY=` (empty).
    """
    pattern = re.compile(rf"^{re.escape(key)}\s*=\s*(.*)$", re.MULTILINE)
    m = pattern.search(text)
    if not m:
        return None
    raw = m.group(1).strip()
    if (raw.startswith('"') and raw.endswith('"')) or (
        raw.startswith("'") and raw.endswith("'")
    ):
        raw = raw[1:-1]
    return raw


# ---------------------------------------------------------------------------
# Individual steps — each returns a result dict and never raises
# ---------------------------------------------------------------------------


def _step_venv_check() -> dict[str, Any]:
    """Warn if not in a venv. Never fatal — user may run system Python."""
    res = _step("venv-check")
    in_venv = sys.prefix != sys.base_prefix
    if in_venv:
        res["status"] = "ok"
        res["detail"] = f"venv active: {sys.prefix}"
    else:
        res["status"] = "warn"
        res["detail"] = (
            "not running inside a venv — recommend "
            "`cd backend && source .venv/bin/activate` before re-running"
        )
        click.echo(
            "[warn] not in a venv — 建议先 `cd backend && source .venv/bin/activate`",
            err=True,
        )
    return res


def _step_env_seed(repo_root: Path) -> dict[str, Any]:
    """Seed ``~/.agentflow/secrets/.env`` from ``backend/.env.template``.

    v1.0.4 moved the canonical secrets file out of the repo and into the
    operator's key folder. This step:

    * Creates ``~/.agentflow/secrets/`` at mode 0700 if missing.
    * Copies ``backend/.env.template`` -> ``~/.agentflow/secrets/.env`` and
      chmods 0600 if the target doesn't already exist.
    * If a legacy ``backend/.env`` exists from a pre-v1.0.4 install AND the
      new target doesn't exist yet, prefers migrating the legacy file
      (preserves operator-set values) over re-seeding from template.
    """
    res = _step("env-seed")
    backend = repo_root / "backend"
    legacy_env_path = backend / ".env"
    template_path = backend / ".env.template"

    secrets_dir = Path.home() / ".agentflow" / "secrets"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    try:
        secrets_dir.chmod(0o700)
    except OSError:
        pass
    target = secrets_dir / ".env"

    if target.exists():
        res["status"] = "skipped"
        res["detail"] = f"{target} already exists"
        return res

    # Prefer migrating an existing legacy .env (operator might have set values
    # before upgrading). Otherwise seed from template.
    if legacy_env_path.exists():
        try:
            shutil.copy2(legacy_env_path, target)
        except OSError as err:
            res["status"] = "failed"
            res["detail"] = f"migrate from legacy failed: {err}"
            return res
        try:
            target.chmod(0o600)
        except OSError:
            pass
        res["status"] = "ok"
        res["detail"] = f"migrated legacy {legacy_env_path} -> {target}"
        return res

    if not template_path.exists():
        res["status"] = "failed"
        res["detail"] = f"template missing: {template_path}"
        return res

    try:
        shutil.copy2(template_path, target)
    except OSError as err:
        res["status"] = "failed"
        res["detail"] = f"copy failed: {err}"
        return res

    try:
        target.chmod(0o600)
    except OSError:
        pass

    res["status"] = "ok"
    res["detail"] = f"created {target} from template"
    return res


def _step_mock_env(env_path: Path) -> dict[str, Any]:
    """Ensure `MOCK_LLM=true` in .env. Triggered by --mock.

    Three states:
      * already correct → "already_set"
      * present but != true → in-place regex replace (preserves line order)
      * absent → append with banner comment
    """
    res = _step("mock-env")
    if not env_path.exists():
        res["status"] = "skipped"
        res["detail"] = f"{env_path} missing — env-seed should have created it"
        return res

    try:
        text = _read_env_text(env_path)
        current = _env_var_value(text, "MOCK_LLM")
        if current is not None and current.strip().lower() == "true":
            res["status"] = "already_set"
            res["detail"] = "MOCK_LLM=true already in .env"
            return res

        if current is not None:
            new_text = _upsert_env_var(text, "MOCK_LLM", "true")
            _write_env_text(env_path, new_text)
            res["status"] = "set"
            res["detail"] = f"replaced MOCK_LLM={current!r} → MOCK_LLM=true"
            return res

        # Append with banner
        if text and not text.endswith("\n"):
            text += "\n"
        text += "\n# Mock mode (set by af bootstrap --mock)\nMOCK_LLM=true\n"
        _write_env_text(env_path, text)
        res["status"] = "set"
        res["detail"] = "appended MOCK_LLM=true"
        return res
    except OSError as err:
        res["status"] = "failed"
        res["detail"] = f"io error: {err}"
        return res


def _step_tg_creds_prompt(env_path: Path) -> dict[str, Any]:
    """Prompt for TG creds when missing. Triggered by --mock + --first-run.

    Either token or chat_id may be left blank — we treat blanks as "user
    will fill later" and don't write anything for that key.
    """
    res = _step("tg-creds")
    if not env_path.exists():
        res["status"] = "skipped"
        res["detail"] = f"{env_path} missing"
        return res

    try:
        text = _read_env_text(env_path)
    except OSError as err:
        res["status"] = "failed"
        res["detail"] = f"io error: {err}"
        return res

    token_existing = _env_var_value(text, "TELEGRAM_BOT_TOKEN") or ""
    chatid_existing = _env_var_value(text, "TELEGRAM_REVIEW_CHAT_ID") or ""

    token_set = False
    chatid_set = False

    # Token prompt (only if missing or empty)
    if not token_existing.strip():
        click.echo(
            "\n📱 Telegram bot token（从 @BotFather 拿；可空跳过 - 后续手填）:"
        )
        try:
            token = click.prompt(
                "TELEGRAM_BOT_TOKEN",
                hide_input=True,
                default="",
                show_default=False,
            ).strip()
        except (click.Abort, EOFError, KeyboardInterrupt):
            token = ""
        if token:
            text = _upsert_env_var(text, "TELEGRAM_BOT_TOKEN", token)
            token_set = True

    # chat_id prompt (only if missing or empty)
    if not chatid_existing.strip():
        click.echo(
            "\n📱 TG chat_id（可空：daemon 启动后给 bot 发 /start 自动捕获）:"
        )
        try:
            chatid = click.prompt(
                "TELEGRAM_REVIEW_CHAT_ID",
                default="",
                show_default=False,
            ).strip()
        except (click.Abort, EOFError, KeyboardInterrupt):
            chatid = ""
        if chatid:
            text = _upsert_env_var(text, "TELEGRAM_REVIEW_CHAT_ID", chatid)
            chatid_set = True

    if token_set or chatid_set:
        try:
            _write_env_text(env_path, text)
        except OSError as err:
            res["status"] = "failed"
            res["detail"] = f"io error: {err}"
            return res

    res["status"] = "ok"
    res["detail"] = (
        f"token_set={token_set} chatid_set={chatid_set} "
        f"(existing token={'yes' if token_existing.strip() else 'no'}, "
        f"existing chatid={'yes' if chatid_existing.strip() else 'no'})"
    )
    res["token_set"] = token_set
    res["chatid_set"] = chatid_set
    return res


def _step_skill_install(mode: str) -> dict[str, Any]:
    """Run `af skill-install --mode <mode>` via subprocess.

    Subprocess-only: directly importing skill_commands.skill_install would
    pull in click's command machinery and require a synthetic Context, which
    is more fragile than just shelling out. We use `_af_argv` so it works
    with or without an installed `af` script.
    """
    res = _step("skill-install")
    argv = _af_argv("skill-install", "--mode", mode)
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as err:
        res["status"] = "failed"
        res["detail"] = f"subprocess error: {err}"
        return res

    if proc.returncode == 0:
        res["status"] = "ok"
        res["detail"] = (proc.stdout.strip().splitlines() or ["installed"])[-1]
    else:
        res["status"] = "failed"
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        res["detail"] = tail[-1] if tail else f"exit code {proc.returncode}"
    return res


def _step_run_onboard() -> dict[str, Any]:
    """Run `af onboard` interactively, inheriting tty.

    Triggered by --first-run AND not --mock.

    We deliberately do NOT capture stdout/stderr/stdin: click prompts inside
    `af onboard` need direct access to the controlling terminal so the user
    can read questions and type answers. `subprocess.run` without any
    stream-redirection kwargs inherits the parent's stdio fds, which is what
    we want.
    """
    res = _step("onboard")
    argv = _af_argv("onboard")
    try:
        proc = subprocess.run(argv, check=False)
    except (OSError, KeyboardInterrupt) as err:
        res["status"] = "failed"
        res["detail"] = f"subprocess error: {err}"
        res["returncode"] = -1
        return res

    res["returncode"] = proc.returncode
    if proc.returncode == 0:
        res["status"] = "ok"
        res["detail"] = "onboard completed"
    else:
        res["status"] = "failed"
        res["detail"] = f"onboard exited with code {proc.returncode}"
    return res


def _step_onboard_pointer() -> dict[str, Any]:
    """Print onboard guidance — never auto-run (it's interactive)."""
    res = _step("onboard-pointer")
    msg = (
        "下一步: 跑 `af onboard` 凭据向导 (10 sections), "
        "或手 vim backend/.env 后跑 `af doctor` 验"
    )
    click.echo(msg)
    res["status"] = "info"
    res["detail"] = msg
    return res


def _step_profile_pointer() -> dict[str, Any]:
    """Print optional profile / style guidance."""
    res = _step("profile-pointer")
    msg = (
        "可选: `af topic-profile init -i --profile <id>` + "
        "`af learn-from-handle <handle> --profile <id>`"
    )
    click.echo(msg)
    res["status"] = "info"
    res["detail"] = msg
    return res


def _step_start_daemon() -> dict[str, Any]:
    """Spawn `af review-daemon` detached and verify heartbeat within 30s.

    Detached via `start_new_session=True` + DEVNULL on all three std streams,
    so the daemon survives this bootstrap process exiting and never blocks
    us waiting for I/O.
    """
    res = _step("daemon")
    argv = _af_argv("review-daemon")
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except (OSError, ValueError) as err:
        res["status"] = "spawn_error"
        res["detail"] = f"could not spawn review-daemon: {err}"
        res["error"] = str(err)
        return res

    hb_path = Path(os.path.expanduser("~/.agentflow/review/last_heartbeat.json"))
    for _ in range(30):
        time.sleep(1)
        if hb_path.exists():
            try:
                data = json.loads(hb_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            ts_str = data.get("timestamp", "") if isinstance(data, dict) else ""
            if not ts_str:
                continue
            try:
                ts = datetime.fromisoformat(ts_str)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                age = (datetime.now(timezone.utc) - ts).total_seconds()
            except ValueError:
                continue
            if age < 60:
                res["status"] = "healthy"
                res["pid"] = proc.pid
                res["heartbeat_age_seconds"] = int(age)
                res["detail"] = (
                    f"daemon pid={proc.pid} heartbeat fresh ({int(age)}s old)"
                )
                return res

    res["status"] = "no_heartbeat"
    res["pid"] = proc.pid
    res["hint"] = (
        "daemon 启动后 30s 内没写心跳; 检查 .env / af doctor"
    )
    res["detail"] = f"daemon pid={proc.pid} but no heartbeat after 30s"
    return res


def _detect_next_step(env_path: Path) -> dict[str, Any]:
    """Detect current init state and return the next command for the user.

    Used by `af bootstrap --next-step` so an AI / skill can orchestrate the
    init path without ever receiving the user's credentials. Each branch is
    graceful — IO errors collapse to ``unknown`` rather than raising.

    Detection order (first match wins):

      1. .env missing                      → cp .env.template .env
      2. TELEGRAM_BOT_TOKEN empty          → af onboard --section telegram
      3. config.json missing or chat_id    → user sends /start to bot
         empty
      4. ~/.claude/skills or ~/.cursor/    → af skill-install
         skills missing
      5. real keys requested but missing   → af onboard --section <missing>
         (LLM_PROVIDER set + MOCK_LLM!=true)
      6. topic_profiles.yaml missing or no → af topic-profile init -i
         healthy profile (brand+voice+do/   --profile <id>
         dont/product_facts/keyword_groups)
      7. heartbeat missing or > 5min stale → af review-daemon &
      8. all good                          → af hotspots --gate-a-top-k 3
    """
    # 1. .env existence
    if not env_path.exists():
        return {
            "current_state": "no_env",
            "next_command": "cp .env.template .env",
            "reason": f".env missing at {env_path}",
            "stage": "init",
        }

    # Read .env once for steps 2, 5
    try:
        env_text = _read_env_text(env_path)
    except OSError as err:
        return {
            "current_state": "unknown",
            "next_command": "af doctor",
            "reason": f"could not read {env_path}: {err}",
            "stage": "init",
        }

    # 2. TELEGRAM_BOT_TOKEN
    tg_token = (_env_var_value(env_text, "TELEGRAM_BOT_TOKEN") or "").strip()
    if not tg_token:
        return {
            "current_state": "missing_telegram_token",
            "next_command": "af onboard --section telegram",
            "reason": "TELEGRAM_BOT_TOKEN is empty in .env",
            "stage": "init",
        }

    # 3. config.json + review_chat_id
    config_path = Path(os.path.expanduser("~/.agentflow/review/config.json"))
    chat_id_ok = False
    try:
        if config_path.exists():
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(cfg, dict):
                chat_id = cfg.get("review_chat_id")
                if chat_id is not None and str(chat_id).strip():
                    chat_id_ok = True
    except (OSError, ValueError):
        chat_id_ok = False

    if not chat_id_ok:
        return {
            "current_state": "missing_chat_id",
            "next_command": (
                "send /start to your bot in Telegram (auto-captures chat_id)"
            ),
            "reason": (
                f"{config_path} missing or review_chat_id empty"
            ),
            "stage": "init",
        }

    # 4. skill harness install
    claude_skills = Path(os.path.expanduser("~/.claude/skills"))
    cursor_skills = Path(os.path.expanduser("~/.cursor/skills"))
    try:
        claude_ok = claude_skills.exists() and any(claude_skills.iterdir())
    except OSError:
        claude_ok = False
    try:
        cursor_ok = cursor_skills.exists() and any(cursor_skills.iterdir())
    except OSError:
        cursor_ok = False

    if not (claude_ok or cursor_ok):
        return {
            "current_state": "skills_not_installed",
            "next_command": "af skill-install",
            "reason": (
                "neither ~/.claude/skills nor ~/.cursor/skills has any skills"
            ),
            "stage": "init",
        }

    # 5. Real-key check (only if MOCK_LLM != true and LLM_PROVIDER is set)
    mock_llm = (_env_var_value(env_text, "MOCK_LLM") or "").strip().lower()
    llm_provider = (_env_var_value(env_text, "LLM_PROVIDER") or "").strip()
    if mock_llm != "true" and llm_provider:
        # Cheap, conservative probe: only flag the obvious LLM-key-by-provider.
        # Detailed key matrix lives in `af doctor`; we just point at it here.
        provider_key_map = {
            "openai": "OPENAI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "deepseek": "DEEPSEEK_API_KEY",
            "openrouter": "OPENROUTER_API_KEY",
        }
        expected_key = provider_key_map.get(llm_provider.lower())
        if expected_key:
            val = (_env_var_value(env_text, expected_key) or "").strip()
            if not val:
                return {
                    "current_state": "missing_real_keys",
                    "next_command": (
                        f"af onboard --section {llm_provider.lower()}"
                    ),
                    "reason": (
                        f"LLM_PROVIDER={llm_provider} set but "
                        f"{expected_key} empty (run af doctor for full matrix)"
                    ),
                    "stage": "init",
                }

    # 6. topic_profile 完整度
    try:
        import yaml
        profiles_yaml = Path(os.path.expanduser("~/.agentflow/topic_profiles.yaml"))
        if not profiles_yaml.exists():
            return {
                "current_state": "missing_profile",
                "next_command": "af topic-profile init -i --profile <name>",
                "reason": "~/.agentflow/topic_profiles.yaml does not exist",
                "stage": "init",
            }
        with profiles_yaml.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        profiles = (data.get("profiles") or {}) if isinstance(data, dict) else {}
        if not profiles:
            return {
                "current_state": "missing_profile",
                "next_command": "af topic-profile init -i --profile <name>",
                "reason": "No profile defined in ~/.agentflow/topic_profiles.yaml",
                "stage": "init",
            }
        # 至少 1 个 profile 含完整 publisher_account
        healthy = False
        for pid, p in profiles.items():
            pa = (p.get("publisher_account") or {}) if isinstance(p, dict) else {}
            do = pa.get("do") or []
            dont = pa.get("dont") or []
            product_facts = pa.get("product_facts") or []
            keyword_groups = (p.get("keyword_groups") or {}) if isinstance(p, dict) else {}
            if (
                pa.get("brand")
                and pa.get("voice")
                and len(do) >= 2
                and len(dont) >= 2
                and len(product_facts) >= 3
                and len(keyword_groups) >= 3
            ):
                healthy = True
                break
        if not healthy:
            return {
                "current_state": "incomplete_profile",
                "next_command": "af topic-profile init -i --profile <id>  # 或 af topic-profile derive --profile <id>",
                "reason": "no profile in topic_profiles.yaml has complete publisher_account (need brand+voice+do[2]+dont[2]+product_facts[3]+keyword_groups[3])",
                "stage": "init",
            }
    except Exception as err:
        pass  # graceful: 解析失败 fall through 到 daemon 检查

    # 7. heartbeat freshness
    hb_path = Path(os.path.expanduser("~/.agentflow/review/last_heartbeat.json"))
    hb_fresh = False
    if hb_path.exists():
        try:
            data = json.loads(hb_path.read_text(encoding="utf-8"))
            ts_str = data.get("timestamp", "") if isinstance(data, dict) else ""
            if ts_str:
                ts = datetime.fromisoformat(ts_str)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                age = (datetime.now(timezone.utc) - ts).total_seconds()
                if age < 300:  # 5 min
                    hb_fresh = True
        except (OSError, ValueError):
            hb_fresh = False

    if not hb_fresh:
        return {
            "current_state": "daemon_not_running",
            "next_command": "af review-daemon &  # or systemd / launchd",
            "reason": (
                f"{hb_path} missing or older than 5min"
            ),
            "stage": "init",
        }

    # 8. all good
    return {
        "current_state": "ready",
        "next_command": "af hotspots --gate-a-top-k 3  # 扫第一批 hotspot",
        "reason": "all init checks pass",
        "stage": "operational",
    }


def _step_kick_hotspots() -> dict[str, Any]:
    """Fire-and-forget `af hotspots --gate-a-top-k 3` after daemon healthy."""
    res = _step("hotspots-kick")
    argv = _af_argv("hotspots", "--gate-a-top-k", "3")
    try:
        subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except (OSError, ValueError) as err:
        res["status"] = "error"
        res["detail"] = f"spawn failed: {err}"
        res["error"] = str(err)
        return res

    res["status"] = "spawned"
    res["hint"] = (
        "Gate A 卡应在 60-90s 后到 TG (假设 daemon 已 capture chat_id)"
    )
    res["detail"] = "hotspots kicked (Gate A top-3)"
    return res


# ---------------------------------------------------------------------------
# Summary rendering
# ---------------------------------------------------------------------------


_STATUS_MARKER = {
    "ok": "OK ",
    "skipped": "-- ",
    "warn": "!! ",
    "failed": "XX ",
    "info": ">> ",
    "pending": ".. ",
    "set": "OK ",
    "already_set": "-- ",
    "healthy": "OK ",
    "no_heartbeat": "!! ",
    "spawn_error": "XX ",
    "spawned": ">> ",
    "error": "XX ",
}


def _render_summary(
    steps: list[dict[str, Any]],
    *,
    mock: bool,
    first_run: bool,
    start_daemon_flag: bool,
) -> None:
    click.echo("")
    click.echo("bootstrap summary")
    click.echo("-" * 56)
    for s in steps:
        marker = _STATUS_MARKER.get(s["status"], "?? ")
        line = f"{marker}{s['step']:<20} {s['status']}"
        if s.get("detail"):
            line += f"  ({s['detail']})"
        click.echo(line)
    click.echo("-" * 56)

    has_failed = any(s["status"] in {"failed", "spawn_error", "error"} for s in steps)

    if has_failed:
        click.echo("next: see failed step(s) above; re-run `af bootstrap` after fixing")
        return

    daemon_step = next((s for s in steps if s["step"] == "daemon"), None)

    # Mode-specific next-step guidance
    if mock and first_run and start_daemon_flag:
        if daemon_step and daemon_step["status"] == "healthy":
            click.echo(
                "next: 在 Telegram 找你的 bot 发 /start — daemon 会自动捕获 "
                "chat_id；约 60-90s 后 Gate A 卡片应弹出"
            )
        else:
            click.echo(
                "next: daemon 心跳异常 — 跑 `af doctor`，确认后再 "
                "`af review-daemon` 手动启动"
            )
    elif start_daemon_flag:
        if daemon_step and daemon_step["status"] == "healthy":
            click.echo(
                f"next: daemon 已起 (pid={daemon_step.get('pid')}); "
                f"心跳 {daemon_step.get('heartbeat_age_seconds')}s — "
                "可跑 `af hotspots` 触发审"
            )
        else:
            click.echo(
                "next: daemon 起了但心跳没到; 跑 `af doctor` 看 .env 是否齐"
            )
    elif mock:
        click.echo(
            "next: MOCK_LLM=true 已设；"
            "`af bootstrap --start-daemon` 起 daemon 或 `af doctor` 验"
        )
    else:
        click.echo("next: `af onboard` (interactive), then `af doctor`")


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------


@cli.command("bootstrap")
@click.option(
    "--skip-skills",
    "skip_skills",
    is_flag=True,
    default=False,
    help="Skip the `af skill-install` step.",
)
@click.option(
    "--skip-onboard",
    "skip_onboard",
    is_flag=True,
    default=False,
    help="Skip printing the `af onboard` pointer.",
)
@click.option(
    "--skill-mode",
    "skill_mode",
    type=click.Choice(["symlink", "copy"]),
    default="symlink",
    show_default=True,
    help="Mode passed through to `af skill-install`.",
)
@click.option(
    "--mock",
    "mock",
    is_flag=True,
    default=False,
    help="Mock mode: auto-set MOCK_LLM=true + prompt TG creds only",
)
@click.option(
    "--start-daemon",
    "start_daemon_flag",
    is_flag=True,
    default=False,
    help="Spawn af review-daemon after setup, verify heartbeat within 30s",
)
@click.option(
    "--first-run",
    "first_run",
    is_flag=True,
    default=False,
    help="Full first-run automation: onboard + start daemon + kick hotspots once",
)
@click.option(
    "--next-step",
    "next_step",
    is_flag=True,
    default=False,
    help="Detect current init state, output next command to run (no setup)",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit results as JSON.",
)
def bootstrap(
    skip_skills: bool,
    skip_onboard: bool,
    skill_mode: str,
    mock: bool,
    start_daemon_flag: bool,
    first_run: bool,
    next_step: bool,
    as_json: bool,
) -> None:
    """One-shot first-run setup: venv check, .env seed, skills, onboard hint.

    Each step runs independently — a failure in one does not abort the rest.

    Flags:

    \b
      --mock            auto MOCK_LLM=true (and prompt TG creds w/ --first-run)
      --start-daemon    spawn review-daemon detached + verify heartbeat (30s)
      --first-run       full automation: run af onboard, start daemon,
                        kick a hotspots Gate-A pass once
      --next-step       detect current init state + emit next command
                        (no setup; safe for AI to call repeatedly)
      --json            machine-readable output
    """
    repo_root = _repo_root()
    # v1.0.4: env_path is now operator's key folder; legacy backend/.env still
    # works as a fallback when present.
    env_path = Path.home() / ".agentflow" / "secrets" / ".env"
    legacy_env_path = repo_root / "backend" / ".env"
    if not env_path.exists() and legacy_env_path.exists():
        env_path = legacy_env_path

    # --next-step: pure read-only state detection, no setup
    if next_step:
        result = _detect_next_step(env_path)
        if as_json:
            _emit_json(result)
        else:
            click.echo(f"current state: {result['current_state']}")
            click.echo(f"next command:  {result['next_command']}")
            click.echo(f"reason:        {result['reason']}")
        return

    steps: list[dict[str, Any]] = []

    # 1. venv check
    steps.append(_step_venv_check())

    # 2. .env seed
    steps.append(_step_env_seed(repo_root))

    # 2b. --mock: auto-set MOCK_LLM=true
    if mock:
        steps.append(_step_mock_env(env_path))

    # 2c. --mock + --first-run: prompt for TG creds (interactive)
    if mock and first_run:
        steps.append(_step_tg_creds_prompt(env_path))

    # 3. skill harness
    if skip_skills:
        steps.append(
            {"step": "skill-install", "status": "skipped", "detail": "--skip-skills"}
        )
    else:
        steps.append(_step_skill_install(skill_mode))

    # 4. onboard
    #    --first-run + not --mock → really run it (interactive, inherits tty)
    #    --skip-onboard           → skipped
    #    else                     → print pointer only
    if first_run and not mock:
        if skip_onboard:
            steps.append(
                {"step": "onboard", "status": "skipped", "detail": "--skip-onboard"}
            )
        else:
            steps.append(_step_run_onboard())
    else:
        if skip_onboard:
            steps.append(
                {
                    "step": "onboard-pointer",
                    "status": "skipped",
                    "detail": "--skip-onboard",
                }
            )
        else:
            steps.append(_step_onboard_pointer())

    # 5. profile / style pointer (always)
    steps.append(_step_profile_pointer())

    # 6. daemon: --start-daemon OR --first-run
    daemon_should_start = start_daemon_flag or first_run
    daemon_result: dict[str, Any] | None = None
    if daemon_should_start:
        daemon_result = _step_start_daemon()
        steps.append(daemon_result)

    # 7. kick hotspots: --first-run AND daemon healthy
    if first_run and daemon_result is not None and daemon_result.get("status") == "healthy":
        steps.append(_step_kick_hotspots())
    elif first_run:
        steps.append(
            {
                "step": "hotspots-kick",
                "status": "skipped",
                "detail": "daemon not healthy — skipping hotspots kick",
            }
        )

    if as_json:
        _emit_json(
            {
                "repo_root": str(repo_root),
                "skill_mode": skill_mode,
                "skip_skills": skip_skills,
                "skip_onboard": skip_onboard,
                "mock": mock,
                "start_daemon": start_daemon_flag,
                "first_run": first_run,
                "steps": steps,
            }
        )
        return

    _render_summary(
        steps,
        mock=mock,
        first_run=first_run,
        start_daemon_flag=start_daemon_flag,
    )

    # Exit non-zero only if a step truly failed (warn/skipped/info do NOT count;
    # no_heartbeat is also tolerated — daemon is up, just slow).
    fatal = {"failed", "spawn_error", "error"}
    if any(s["status"] in fatal for s in steps):
        raise click.ClickException("one or more bootstrap steps failed")


__all__ = ["bootstrap"]
