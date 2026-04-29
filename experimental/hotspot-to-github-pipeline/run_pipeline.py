#!/usr/bin/env python3
"""Execute Hotspot To GitHub pipeline cases with discovery and writeback."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent
DEFAULT_WORKSPACE_ROOT = ROOT / "workspaces"
DEFAULT_POOL_FILE = ROOT / "pipeline-pool.md"


class PipelineError(RuntimeError):
    """Raised for pipeline execution errors."""


def iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    return value or "hotspot"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a Hotspot To GitHub pipeline case.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--case-dir", help="Case directory containing 02-pipeline-gate.yaml")
    group.add_argument("--gate-file", help="Path to a pipeline gate YAML file")
    parser.add_argument(
        "--mode",
        default="inspect",
        choices=["inspect", "discover", "probe", "publish"],
        help="Execution mode. Default: inspect",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually run commands and write back results. Without this flag the tool prints a dry-run plan.",
    )
    parser.add_argument(
        "--allow-publish",
        action="store_true",
        help="Required together with --execute when mode=publish.",
    )
    parser.add_argument(
        "--workspace-root",
        default=str(DEFAULT_WORKSPACE_ROOT),
        help="Directory used for local workspaces.",
    )
    parser.add_argument(
        "--candidate-index",
        type=int,
        default=0,
        help="Which candidate repo to use for fork_existing/template_clone. Default: 0",
    )
    parser.add_argument(
        "--discover-query",
        default="",
        help="Optional override query used for candidate repo discovery.",
    )
    parser.add_argument(
        "--discover-limit",
        type=int,
        default=5,
        help="Max number of candidate repos to discover.",
    )
    parser.add_argument(
        "--pool-file",
        default=str(DEFAULT_POOL_FILE),
        help="Pipeline pool markdown file to update during writeback.",
    )
    parser.add_argument(
        "--no-writeback",
        action="store_true",
        help="Execute commands but do not persist results back to gate/probe-run/pool files.",
    )
    return parser.parse_args()


def load_gate_file(args: argparse.Namespace) -> tuple[Path, dict]:
    gate_file = Path(args.gate_file).expanduser().resolve() if args.gate_file else (
        Path(args.case_dir).expanduser().resolve() / "02-pipeline-gate.yaml"
    )
    if not gate_file.exists():
        raise PipelineError(f"Gate file not found: {gate_file}")
    with gate_file.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    return gate_file, data


def dump_gate_file(path: Path, config: dict) -> None:
    with path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(config, handle, allow_unicode=True, sort_keys=False)


def require_string(mapping: dict, *keys: str) -> str:
    current = mapping
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return ""
        current = current[key]
    return current if isinstance(current, str) else ""


def ensure_nested_dict(mapping: dict, *keys: str) -> dict:
    current = mapping
    for key in keys:
        value = current.get(key)
        if not isinstance(value, dict):
            value = {}
            current[key] = value
        current = value
    return current


def candidate_repo(config: dict, index: int) -> dict:
    items = config.get("gate_3_repo_routing", {}).get("candidate_repos", [])
    if not items:
        return {}
    if index < 0 or index >= len(items):
        raise PipelineError(f"Candidate repo index out of range: {index}")
    item = items[index]
    return item if isinstance(item, dict) else {}


def case_dir_from_gate(gate_file: Path) -> Path:
    return gate_file.parent


def probe_run_file(case_dir: Path) -> Path:
    return case_dir / "04-build-probe-run.md"


def workspace_dir(config: dict, workspace_root: Path) -> Path:
    repo_plan = config.get("repo_plan", {})
    explicit = repo_plan.get("local_workspace", "")
    if explicit:
        return Path(explicit).expanduser().resolve()
    hotspot_id = require_string(config, "meta", "hotspot_id") or "HSP-NEW"
    hotspot_name = require_string(config, "meta", "hotspot_name") or "hotspot"
    date_value = require_string(config, "meta", "date") or "undated"
    return workspace_root.expanduser().resolve() / f"{hotspot_id}-{date_value}-{slugify(hotspot_name)}"


def repo_name(config: dict) -> str:
    explicit = require_string(config, "repo_plan", "repo_name")
    if explicit:
        return explicit
    hotspot_name = require_string(config, "meta", "hotspot_name") or "hotspot"
    return slugify(hotspot_name)


def repo_owner(config: dict) -> str:
    return require_string(config, "repo_plan", "github_owner")


def repo_visibility(config: dict) -> str:
    visibility = require_string(config, "repo_plan", "visibility") or "public"
    if visibility not in {"public", "private"}:
        raise PipelineError(f"Unsupported visibility: {visibility}")
    return visibility


def default_branch(config: dict) -> str:
    return require_string(config, "repo_plan", "default_branch") or "main"


def run_command(command: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=False)


def ensure_empty_workspace(target: Path) -> None:
    if target.exists():
        raise PipelineError(f"Workspace already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)


def print_section(title: str) -> None:
    print(f"\n== {title} ==")


def print_plan(config: dict, gate_file: Path, workspace: Path, candidate: dict) -> None:
    hotspot_id = require_string(config, "meta", "hotspot_id")
    hotspot_name = require_string(config, "meta", "hotspot_name")
    status = require_string(config, "decision", "final_status")
    strategy = require_string(config, "gate_3_repo_routing", "repo_strategy")
    shape = require_string(config, "gate_2_project_shape", "project_shape")
    print_section("Pipeline Summary")
    print(f"Gate file: {gate_file}")
    print(f"Hotspot: {hotspot_id} {hotspot_name}")
    print(f"Status: {status}")
    print(f"Project shape: {shape}")
    print(f"Repo strategy: {strategy}")
    print(f"Workspace: {workspace}")
    if candidate:
        print(f"Candidate repo: {candidate.get('name', '')} {candidate.get('url', '')}")
    commands = config.get("gate_4_buildability", {}).get("build_commands", {})
    if isinstance(commands, dict):
        print(f"Install command: {commands.get('install', '')}")
        print(f"Build command: {commands.get('build', '')}")
        print(f"Test command: {commands.get('test', '')}")


def dedupe_terms(parts: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for part in parts:
        normalized = part.strip()
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


def discovery_query(config: dict, override: str) -> str:
    if override:
        return override
    saved = require_string(config, "gate_3_repo_routing", "discovered_query")
    if saved:
        return saved
    parts = [require_string(config, "meta", "hotspot_name")]
    topic_lineage = config.get("source_context", {}).get("topic_lineage", [])
    if isinstance(topic_lineage, list):
        parts.extend(item for item in topic_lineage if isinstance(item, str))
    shape = require_string(config, "gate_2_project_shape", "project_shape")
    if shape and shape != "undecided":
        parts.append(shape.replace("_", " "))
    return " ".join(dedupe_terms(parts)).strip()


def gh_search_repos(query: str, limit: int) -> list[dict]:
    command = [
        "gh",
        "search",
        "repos",
        query,
        "--limit",
        str(limit),
        "--json",
        "name,owner,url,description,stargazersCount,updatedAt,license",
    ]
    result = run_command(command)
    if result.returncode != 0:
        raise PipelineError(result.stderr.strip() or "gh search repos failed")
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise PipelineError(f"Unable to parse GitHub search output: {exc}") from exc
    if not isinstance(payload, list):
        raise PipelineError("Unexpected GitHub search payload shape.")
    return payload


def owner_login(item: dict) -> str:
    owner = item.get("owner")
    if isinstance(owner, dict):
        return str(owner.get("login", "")).strip()
    return str(owner or "").strip()


def normalize_license(item: dict) -> str:
    license_info = item.get("license")
    if isinstance(license_info, dict):
        return str(
            license_info.get("spdx_id")
            or license_info.get("spdxId")
            or license_info.get("name")
            or ""
        ).strip()
    return ""


def parse_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def score_candidate(config: dict, candidate: dict) -> tuple[int, str]:
    hotspot_slug = slugify(require_string(config, "meta", "hotspot_name"))
    shape = require_string(config, "gate_2_project_shape", "project_shape")
    name_slug = slugify(str(candidate.get("name", "")))
    description_slug = slugify(str(candidate.get("description", "")))
    score = 0
    reasons: list[str] = []
    if hotspot_slug and hotspot_slug in name_slug:
        score += 30
        reasons.append("名称贴近热点")
    if shape != "undecided":
        shape_token = shape.replace("_", "-")
        if shape_token in name_slug or shape_token in description_slug:
            score += 20
            reasons.append("项目形态匹配")
    stars = int(candidate.get("stars", 0) or 0)
    if stars >= 500:
        score += 20
        reasons.append("stars 很高")
    elif stars >= 100:
        score += 15
        reasons.append("stars 较高")
    elif stars >= 20:
        score += 10
        reasons.append("有基础社区验证")
    updated_at = parse_timestamp(str(candidate.get("updated_at", "")))
    if updated_at:
        age_days = max((datetime.now(timezone.utc) - updated_at.astimezone(timezone.utc)).days, 0)
        if age_days <= 30:
            score += 15
            reasons.append("近期活跃")
        elif age_days <= 90:
            score += 10
            reasons.append("近三个月活跃")
        elif age_days <= 180:
            score += 5
            reasons.append("半年内有更新")
    license_note = str(candidate.get("license_note", "")).lower()
    if license_note:
        if "mit" in license_note or "apache" in license_note or "bsd" in license_note:
            score += 10
            reasons.append("许可友好")
        else:
            score += 3
            reasons.append("许可已知")
    else:
        reasons.append("许可未知")
    if not reasons:
        reasons.append("仅基于基础匹配")
    return score, "，".join(reasons)


def normalize_candidates(raw_items: list[dict], query: str, config: dict) -> list[dict]:
    candidates: list[dict] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        owner = owner_login(item)
        name = str(item.get("name", "")).strip()
        full_name = f"{owner}/{name}".strip("/") if owner else name
        candidate = {
            "name": full_name,
            "url": str(item.get("url", "")).strip(),
            "description": str(item.get("description", "") or "").strip(),
            "stars": int(item.get("stargazersCount", 0) or 0),
            "updated_at": str(item.get("updatedAt", "") or "").strip(),
            "fit_reason": f"Discovered from GitHub search query: {query}",
            "license_note": normalize_license(item),
        }
        score, ranking_reason = score_candidate(config, candidate)
        candidate["score"] = score
        candidate["ranking_reason"] = ranking_reason
        candidates.append(
            candidate
        )
    candidates.sort(key=lambda item: (-int(item.get("score", 0) or 0), -int(item.get("stars", 0) or 0), str(item.get("name", ""))))
    return candidates


def recommend_strategy(config: dict, candidates: list[dict]) -> tuple[str, str]:
    hotspot_name = slugify(require_string(config, "meta", "hotspot_name"))
    shape = require_string(config, "gate_2_project_shape", "project_shape")
    if not candidates:
        return "new_repo", "No viable GitHub candidates were discovered, so starting a new repo is the safest default."
    top_score = int(candidates[0].get("score", 0) or 0)
    for candidate in candidates:
        candidate_name = slugify(str(candidate.get("name", "")))
        if hotspot_name and hotspot_name in candidate_name:
            return "fork_existing", "A close name match exists on GitHub, so fork_existing is the most direct route."
    if shape in {"demo", "starter", "agent_workflow", "mcp_server"} and top_score >= 30:
        return "template_clone", "Template-friendly project shape with viable upstream repos; template_clone is the best starting point."
    return "new_repo", "Project shape favors a clean public API or controlled structure, so new_repo is the safer default."


def ensure_execution_state(config: dict) -> dict:
    return ensure_nested_dict(config, "execution_state")


def discover_candidates(config: dict, query: str, limit: int) -> tuple[list[dict], str, str]:
    candidates = normalize_candidates(gh_search_repos(query, limit), query, config)
    strategy, reason = recommend_strategy(config, candidates)
    gate = ensure_nested_dict(config, "gate_3_repo_routing")
    gate["discovered_query"] = query
    gate["recommended_strategy"] = strategy
    gate["recommended_reason"] = reason
    gate["candidate_repos"] = candidates
    if require_string(config, "gate_3_repo_routing", "repo_strategy") == "undecided":
        gate["repo_strategy"] = strategy
    execution_state = ensure_execution_state(config)
    discovery_state = ensure_nested_dict(execution_state, "discovery")
    discovery_state["last_run_at"] = iso_now()
    discovery_state["query"] = query
    discovery_state["discovered_count"] = len(candidates)
    if candidates:
        discovery_state["selected_candidate_name"] = str(candidates[0].get("name", ""))
        discovery_state["selected_candidate_url"] = str(candidates[0].get("url", ""))
        discovery_state["selected_candidate_index"] = 0
    else:
        discovery_state["selected_candidate_name"] = ""
        discovery_state["selected_candidate_url"] = ""
        discovery_state["selected_candidate_index"] = -1
    return candidates, strategy, reason


def prepare_new_repo_workspace(config: dict, workspace: Path) -> None:
    ensure_empty_workspace(workspace)
    workspace.mkdir(parents=True, exist_ok=False)
    hotspot_name = require_string(config, "meta", "hotspot_name")
    thesis = require_string(config, "decision", "one_line_thesis")
    readme = f"# {hotspot_name}\n\n{thesis or 'Generated by Hotspot To GitHub Pipeline.'}\n"
    notes = (
        "# Pipeline Notes\n\n"
        f"- Hotspot ID: {require_string(config, 'meta', 'hotspot_id')}\n"
        f"- Repo strategy: {require_string(config, 'gate_3_repo_routing', 'repo_strategy')}\n"
        f"- Project shape: {require_string(config, 'gate_2_project_shape', 'project_shape')}\n"
    )
    (workspace / "README.md").write_text(readme, encoding="utf-8")
    (workspace / "PIPELINE_NOTES.md").write_text(notes, encoding="utf-8")


def prepare_cloned_workspace(workspace: Path, url: str) -> None:
    ensure_empty_workspace(workspace)
    result = run_command(["git", "clone", url, str(workspace)])
    if result.returncode != 0:
        raise PipelineError(result.stderr.strip() or "git clone failed")


def prepare_workspace(config: dict, workspace: Path, candidate: dict, execute: bool) -> list[str]:
    strategy = require_string(config, "gate_3_repo_routing", "repo_strategy")
    actions: list[str] = []
    if strategy == "new_repo":
        actions.append(f"mkdir {workspace}")
        actions.append("seed README.md and PIPELINE_NOTES.md")
        if execute:
            prepare_new_repo_workspace(config, workspace)
    elif strategy in {"fork_existing", "template_clone"}:
        url = candidate.get("url", "")
        if not url:
            raise PipelineError(f"{strategy} requires candidate_repos[0].url")
        actions.append(f"git clone {url} {workspace}")
        if execute:
            prepare_cloned_workspace(workspace, url)
    else:
        raise PipelineError(f"Unsupported or undecided repo strategy: {strategy}")
    return actions


def run_shell_command(command: str, cwd: Path, execute: bool) -> dict:
    if not command:
        return {"status": "skipped", "stdout": "", "stderr": "", "command": ""}
    if not execute:
        return {"status": "planned", "stdout": "", "stderr": "", "command": command}
    result = subprocess.run(command, cwd=cwd, shell=True, text=True, capture_output=True, check=False)
    return {
        "status": "passed" if result.returncode == 0 else "failed",
        "stdout": result.stdout,
        "stderr": result.stderr,
        "command": command,
    }


def run_probe(config: dict, workspace: Path, execute: bool) -> dict:
    commands = config.get("gate_4_buildability", {}).get("build_commands", {})
    if not isinstance(commands, dict):
        commands = {}
    results = {
        "install": run_shell_command(str(commands.get("install", "") or ""), workspace, execute),
        "build": run_shell_command(str(commands.get("build", "") or ""), workspace, execute),
        "test": run_shell_command(str(commands.get("test", "") or ""), workspace, execute),
    }
    failures = [step for step, result in results.items() if result["status"] == "failed"]
    if failures:
        failure_details = []
        for step in failures:
            entry = results[step]
            failure_details.append(
                f"Command failed in {workspace}:\n$ {entry['command']}\n\nstdout:\n{entry['stdout']}\n\nstderr:\n{entry['stderr']}"
            )
        raise PipelineError("\n\n".join(failure_details))
    return results


def ensure_git_user_configured() -> None:
    name = run_command(["git", "config", "--get", "user.name"])
    email = run_command(["git", "config", "--get", "user.email"])
    if name.returncode != 0 or email.returncode != 0 or not name.stdout.strip() or not email.stdout.strip():
        raise PipelineError("Git user.name and user.email must be configured before publish.")


def remove_existing_git_dir(workspace: Path) -> None:
    git_dir = workspace / ".git"
    if git_dir.exists():
        shutil.rmtree(git_dir)


def initialize_git_repo(workspace: Path, branch: str) -> None:
    ensure_git_user_configured()
    remove_existing_git_dir(workspace)
    result = run_command(["git", "init", "-b", branch], cwd=workspace)
    if result.returncode != 0:
        fallback = run_command(["git", "init"], cwd=workspace)
        if fallback.returncode != 0:
            raise PipelineError(fallback.stderr.strip() or "git init failed")
        checkout = run_command(["git", "checkout", "-b", branch], cwd=workspace)
        if checkout.returncode != 0:
            raise PipelineError(checkout.stderr.strip() or "git checkout -b failed")
    add_result = run_command(["git", "add", "."], cwd=workspace)
    if add_result.returncode != 0:
        raise PipelineError(add_result.stderr.strip() or "git add failed")
    status = run_command(["git", "status", "--porcelain"], cwd=workspace)
    if status.returncode != 0:
        raise PipelineError(status.stderr.strip() or "git status failed")
    if status.stdout.strip():
        commit = run_command(["git", "commit", "-m", "Initialize hotspot pipeline workspace"], cwd=workspace)
        if commit.returncode != 0:
            raise PipelineError(commit.stderr.strip() or "git commit failed")


def current_gh_login() -> str:
    result = run_command(["gh", "api", "user", "--jq", ".login"])
    if result.returncode != 0:
        raise PipelineError(result.stderr.strip() or "Unable to resolve current GitHub user.")
    return result.stdout.strip()


def publish_workspace(config: dict, workspace: Path, candidate: dict, execute: bool, allow_publish: bool) -> tuple[list[str], str]:
    if not execute:
        owner = repo_owner(config) or "<github-owner>"
        return ["dry-run publish plan generated"], f"{owner}/{repo_name(config)}"
    if not allow_publish:
        raise PipelineError("Publishing requires --allow-publish together with --execute.")

    strategy = require_string(config, "gate_3_repo_routing", "repo_strategy")
    actions: list[str] = []
    if strategy == "fork_existing":
        url = candidate.get("url", "")
        if not url:
            raise PipelineError("fork_existing requires candidate_repos[0].url")
        result = run_command(["gh", "repo", "fork", url, "--clone=false", "--remote=false"])
        if result.returncode != 0:
            raise PipelineError(result.stderr.strip() or "gh repo fork failed")
        actions.append("gh repo fork executed")
        repo_ref = str(candidate.get("name", ""))
        return actions, repo_ref

    owner = repo_owner(config) or current_gh_login()
    name = repo_name(config)
    visibility = repo_visibility(config)
    branch = default_branch(config)
    initialize_git_repo(workspace, branch)
    repo_ref = f"{owner}/{name}"
    command = ["gh", "repo", "create", repo_ref, f"--{visibility}", "--source", str(workspace), "--push"]
    result = run_command(command)
    if result.returncode != 0:
        raise PipelineError(result.stderr.strip() or "gh repo create failed")
    actions.append(f"gh repo create {repo_ref} executed")
    return actions, repo_ref


def command_status(result: dict) -> str:
    status = str(result.get("status", "not_run"))
    if status == "planned":
        return "not_run"
    return status


def derive_gate4_verdict(results: dict) -> str:
    statuses = {command_status(item) for item in results.values()}
    if "failed" in statuses:
        return "fail"
    if "passed" in statuses and statuses.issubset({"passed", "skipped"}):
        return "pass"
    return "hold"


def replace_line(text: str, prefix: str, new_line: str) -> str:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith(prefix):
            lines[index] = new_line
            return "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    return text


def replace_enumerated_line(text: str, index: int, new_line: str) -> str:
    marker = f"{index}."
    lines = text.splitlines()
    for line_index, line in enumerate(lines):
        if line.startswith(marker):
            lines[line_index] = new_line
            return "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    return text


def replace_section_numbered_items(text: str, header: str, items: list[str], count: int = 3) -> str:
    lines = text.splitlines()
    start = None
    for idx, line in enumerate(lines):
        if line.strip() == header:
            start = idx + 1
            break
    if start is None:
        return text
    numbered_seen = 0
    for idx in range(start, len(lines)):
        stripped = lines[idx].strip()
        if stripped.startswith("## ") and idx > start:
            break
        for number in range(1, count + 1):
            if stripped.startswith(f"{number}."):
                value = items[number - 1] if number - 1 < len(items) else ""
                lines[idx] = f"{number}. {value}" if value else f"{number}."
                numbered_seen += 1
                break
        if numbered_seen >= count:
            break
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def update_probe_run_file(case_dir: Path, config: dict, workspace: Path, candidate: dict, results: dict, mode: str) -> None:
    path = probe_run_file(case_dir)
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    text = replace_line(text, "- Hotspot ID:", f"- Hotspot ID: {require_string(config, 'meta', 'hotspot_id')}")
    text = replace_line(text, "- Hotspot:", f"- Hotspot: {require_string(config, 'meta', 'hotspot_name')}")
    text = replace_line(text, "- Date:", f"- Date: {require_string(config, 'meta', 'date')}")
    text = replace_line(text, "- Owner:", f"- Owner: {require_string(config, 'meta', 'owner')}")
    text = replace_line(text, "- Source gate:", f"- Source gate: {require_string(config, 'build_probe', 'source_gate')}")
    text = replace_line(text, "- Repo strategy:", f"- Repo strategy: {require_string(config, 'gate_3_repo_routing', 'repo_strategy')}")
    text = replace_line(text, "- Candidate repo or template:", f"- Candidate repo or template: {candidate.get('name', '') or candidate.get('url', '')}")
    text = replace_line(text, "- Hypothesis:", f"- Hypothesis: {require_string(config, 'build_probe', 'hypothesis')}")
    text = replace_line(text, "- Experiment type:", f"- Experiment type: {require_string(config, 'build_probe', 'experiment_type')}")
    text = replace_line(text, "- Timebox:", f"- Timebox: {require_string(config, 'build_probe', 'timebox')}")
    commands = config.get("gate_4_buildability", {}).get("build_commands", {})
    if not isinstance(commands, dict):
        commands = {}
    clone_command = ""
    strategy = require_string(config, "gate_3_repo_routing", "repo_strategy")
    if strategy in {"fork_existing", "template_clone"} and candidate.get("url"):
        clone_command = f"git clone {candidate.get('url')} {workspace}"
    elif strategy == "new_repo":
        clone_command = f"mkdir {workspace}"
    text = replace_line(text, "- Clone command:", f"- Clone command: {clone_command}")
    text = replace_line(text, "- Install command:", f"- Install command: {commands.get('install', '')}")
    text = replace_line(text, "- Build command:", f"- Build command: {commands.get('build', '')}")
    text = replace_line(text, "- Test command:", f"- Test command: {commands.get('test', '')}")
    success = config.get("build_probe", {}).get("success_signal", [])
    failure = config.get("build_probe", {}).get("failure_signal", [])
    text = replace_line(text, "- Success signal:", f"- Success signal: {success[0] if isinstance(success, list) and success else ''}")
    text = replace_line(text, "- Failure signal:", f"- Failure signal: {failure[0] if isinstance(failure, list) and failure else ''}")
    kill_signals = config.get("gate_4_buildability", {}).get("kill_signals", [])
    text = replace_line(text, "- Kill signal to watch:", f"- Kill signal to watch: {kill_signals[0] if isinstance(kill_signals, list) and kill_signals else ''}")
    text = replace_enumerated_line(text, 1, f"1. Mode: {mode}")
    text = replace_enumerated_line(text, 2, f"2. Workspace: {workspace}")
    text = replace_enumerated_line(text, 3, f"3. Strategy: {strategy}")
    text = replace_line(text, "- Observation 1:", f"- Observation 1: install={command_status(results.get('install', {}))}")
    text = replace_line(text, "- Observation 2:", f"- Observation 2: build={command_status(results.get('build', {}))}")
    text = replace_line(text, "- Observation 3:", f"- Observation 3: test={command_status(results.get('test', {}))}")
    statuses = [command_status(results.get(name, {})) for name in ("install", "build", "test")]
    overall = "strong_pass" if "failed" not in statuses and "passed" in statuses else "mixed"
    if "failed" in statuses:
        overall = "fail"
    text = replace_line(text, "- Result:", f"- Result: {overall}")
    text = replace_line(text, "- Build status:", f"- Build status: {command_status(results.get('build', {}))}")
    text = replace_line(text, "- Test status:", f"- Test status: {command_status(results.get('test', {}))}")
    text = replace_line(text, "- Did we trigger a kill signal:", "- Did we trigger a kill signal: no")
    text = replace_line(text, "- What changed in the pipeline view:", f"- What changed in the pipeline view: buildability verdict now leans {derive_gate4_verdict(results)}")
    next_status = "publish" if mode == "publish" else require_string(config, "decision", "final_status")
    text = replace_line(text, "- Recommended next status:", f"- Recommended next status: {next_status}")
    text = replace_line(text, "- Biggest surprise:", f"- Biggest surprise: selected workspace route was {strategy}")
    text = replace_line(text, "- What we learned:", f"- What we learned: install/build/test statuses are {statuses}")
    text = replace_line(text, "- What remains unresolved:", f"- What remains unresolved: {require_string(config, 'decision', 'primary_constraint')}")
    path.write_text(text, encoding="utf-8")


def update_memo_file(case_dir: Path, config: dict, candidate: dict, results: dict | None = None, mode: str = "inspect") -> None:
    path = case_dir / "03-publish-decision-memo.md"
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    text = replace_line(text, "`Decision`:", f"`Decision`: `{require_string(config, 'decision', 'final_status')}`")
    thesis = require_string(config, "decision", "one_line_thesis")
    if thesis:
        text = replace_line(text, "`One-line thesis`:", f"`One-line thesis`: {thesis}")
    text = replace_line(text, "`Primary constraint`:", f"`Primary constraint`: `{require_string(config, 'decision', 'primary_constraint') or 'unknown'}`")
    text = replace_line(text, "- Hotspot ID:", f"- Hotspot ID: {require_string(config, 'meta', 'hotspot_id')}")
    text = replace_line(text, "- Hotspot:", f"- Hotspot: {require_string(config, 'meta', 'hotspot_name')}")
    text = replace_line(text, "- Original question:", f"- Original question: {require_string(config, 'hotspot_question', 'original_question')}")
    text = replace_line(text, "- Reframed question:", f"- Reframed question: {require_string(config, 'hotspot_question', 'reframed_question')}")
    text = replace_line(text, "- Why repo:", f"- Why repo: {require_string(config, 'hotspot_question', 'why_this_should_be_a_repo')}")
    text = replace_line(text, "- Falsified if:", f"- Falsified if: {', '.join(config.get('hotspot_question', {}).get('falsified_if', []))}")
    text = replace_line(text, "- Project shape:", f"- Project shape: {require_string(config, 'gate_2_project_shape', 'project_shape')}")
    text = replace_line(text, "- Repo strategy:", f"- Repo strategy: {require_string(config, 'gate_3_repo_routing', 'repo_strategy')}")
    text = replace_line(text, "- Candidate repo or template:", f"- Candidate repo or template: {candidate.get('name', '') or candidate.get('url', '')}")
    text = replace_line(text, "- Why this route:", f"- Why this route: {require_string(config, 'gate_3_repo_routing', 'recommended_reason')}")
    text = replace_line(text, "`Veto from gate`:", f"`Veto from gate`: `{require_string(config, 'decision', 'veto_from_gate') or 'none'}`")
    commands = config.get("gate_4_buildability", {}).get("build_commands", {})
    if not isinstance(commands, dict):
        commands = {}
    text = replace_line(text, "`Build command`:", f"`Build command`: {commands.get('build', '')}")
    text = replace_line(text, "`Test command`:", f"`Test command`: {commands.get('test', '')}")
    text = replace_line(text, "`Timebox`:", f"`Timebox`: {require_string(config, 'build_probe', 'timebox')}")
    text = replace_line(text, "`Owner`:", f"`Owner`: {require_string(config, 'meta', 'owner')}")
    success = config.get("build_probe", {}).get("success_signal", [])
    failure = config.get("build_probe", {}).get("failure_signal", [])
    text = replace_line(text, "`Success signal`:", f"`Success signal`: {success[0] if isinstance(success, list) and success else ''}")
    text = replace_line(text, "`Failure signal`:", f"`Failure signal`: {failure[0] if isinstance(failure, list) and failure else ''}")
    text = replace_line(text, "`Next review date`:", f"`Next review date`: {require_string(config, 'decision', 'next_review_date')}")
    text = replace_line(text, "`Previous status`:", f"`Previous status`: {config.get('review_log', [{}])[-1].get('previous_status', require_string(config, 'decision', 'final_status'))}")
    last_review = config.get("review_log", [{}])[-1] if isinstance(config.get("review_log"), list) and config.get("review_log") else {}
    text = replace_line(text, "`What changed since last round`:", f"`What changed since last round`: {last_review.get('what_changed', '')}")
    text = replace_line(text, "`What remains unresolved`:", f"`What remains unresolved`: {require_string(config, 'decision', 'primary_constraint')}")
    text = replace_line(text, "`Lesson so far`:", f"`Lesson so far`: {last_review.get('lessons', '')}")
    strongest = []
    for gate_key in ("gate_1_hotspot_signal", "gate_2_project_shape", "gate_3_repo_routing"):
        evidence = config.get(gate_key, {}).get("evidence", [])
        if isinstance(evidence, list):
            for item in evidence:
                if isinstance(item, dict) and item.get("summary"):
                    strongest.append(str(item["summary"]))
    if candidate.get("ranking_reason"):
        strongest.append(f"Top candidate: {candidate.get('name', '')} ({candidate.get('ranking_reason', '')})")
    text = replace_section_numbered_items(text, "## 5. Strongest Evidence", strongest)
    risks = []
    for gate_key in ("gate_3_repo_routing", "gate_4_buildability", "gate_5_publish_decision"):
        signals = config.get(gate_key, {}).get("kill_signals", [])
        if isinstance(signals, list):
            risks.extend(str(item) for item in signals if item)
    text = replace_section_numbered_items(text, "## 6. Biggest Risks", risks)
    path.write_text(text, encoding="utf-8")


def update_review_checkpoint_file(case_dir: Path, config: dict, candidate: dict) -> None:
    path = case_dir / "05-review-checkpoint.md"
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    review_log = config.get("review_log", [])
    latest = review_log[-1] if isinstance(review_log, list) and review_log else {}
    text = replace_line(text, "- Hotspot ID:", f"- Hotspot ID: {require_string(config, 'meta', 'hotspot_id')}")
    text = replace_line(text, "- Hotspot:", f"- Hotspot: {require_string(config, 'meta', 'hotspot_name')}")
    text = replace_line(text, "- Review date:", f"- Review date: {latest.get('date', '')}")
    text = replace_line(text, "- Reviewer:", f"- Reviewer: {require_string(config, 'meta', 'owner')}")
    text = replace_line(text, "- Previous status:", f"- Previous status: {latest.get('previous_status', '')}")
    text = replace_line(text, "- New status:", f"- New status: {latest.get('new_status', '')}")
    text = replace_line(text, "- New evidence:", f"- New evidence: {candidate.get('name', '') or require_string(config, 'execution_state', 'probe', 'summary')}")
    text = replace_line(text, "- Invalidated assumption:", f"- Invalidated assumption: {require_string(config, 'decision', 'primary_constraint')}")
    text = replace_line(text, "- New repo candidate:", f"- New repo candidate: {candidate.get('name', '') or candidate.get('url', '')}")
    text = replace_line(text, "- New risk:", f"- New risk: {require_string(config, 'decision', 'veto_from_gate')}")
    text = replace_line(text, "- Updated project shape:", f"- Updated project shape: {require_string(config, 'gate_2_project_shape', 'project_shape')}")
    text = replace_line(text, "- Updated repo strategy:", f"- Updated repo strategy: {require_string(config, 'gate_3_repo_routing', 'repo_strategy')}")
    text = replace_line(text, "- Continue / pause / stop:", f"- Continue / pause / stop: {require_string(config, 'decision', 'final_status')}")
    text = replace_line(text, "- Next action:", f"- Next action: {require_string(config, 'decision', 'next_action')}")
    text = replace_line(text, "- Next review date:", f"- Next review date: {require_string(config, 'decision', 'next_review_date')}")
    text = replace_line(text, "- What we got right:", f"- What we got right: {latest.get('what_changed', '')}")
    text = replace_line(text, "- What we got wrong:", f"- What we got wrong: {require_string(config, 'decision', 'veto_from_gate')}")
    text = replace_line(text, "- What we still do not know:", f"- What we still do not know: {require_string(config, 'decision', 'primary_constraint')}")
    path.write_text(text, encoding="utf-8")


def update_gate_after_probe(config: dict, workspace: Path, candidate: dict, results: dict, mode: str, repo_ref: str = "") -> None:
    execution_state = ensure_execution_state(config)
    probe_state = ensure_nested_dict(execution_state, "probe")
    probe_state["last_run_at"] = iso_now()
    probe_state["workspace"] = str(workspace)
    probe_state["install_status"] = command_status(results.get("install", {}))
    probe_state["build_status"] = command_status(results.get("build", {}))
    probe_state["test_status"] = command_status(results.get("test", {}))
    probe_state["summary"] = (
        f"candidate={candidate.get('name', '') or 'none'} "
        f"install={probe_state['install_status']} build={probe_state['build_status']} test={probe_state['test_status']}"
    )
    gate4 = ensure_nested_dict(config, "gate_4_buildability")
    gate4["verdict"] = derive_gate4_verdict(results)
    if gate4["verdict"] == "pass" and int(gate4.get("score", 0) or 0) == 0:
        gate4["score"] = 4
    decision = ensure_nested_dict(config, "decision")
    if mode == "probe" and gate4["verdict"] == "pass" and decision.get("final_status") in {"draft", "watch"}:
        decision["final_status"] = "probe"
        decision["summary"] = "Probe completed successfully; ready for publish decision."
        decision["next_action"] = "review publish readiness and decide whether to publish"
    if gate4["verdict"] == "fail":
        decision["primary_constraint"] = "buildability"
        decision["veto_from_gate"] = "gate_4_buildability"
    publish_state = ensure_nested_dict(execution_state, "publish")
    if mode == "publish":
        publish_state["last_run_at"] = iso_now()
        publish_state["repo_ref"] = repo_ref
        publish_state["publish_status"] = "passed"
        publish_state["summary"] = f"Published via {require_string(config, 'gate_3_repo_routing', 'repo_strategy')} to {repo_ref}"
        gate5 = ensure_nested_dict(config, "gate_5_publish_decision")
        gate5["verdict"] = "pass"
        if int(gate5.get("score", 0) or 0) == 0:
            gate5["score"] = 4
        decision["final_status"] = "publish"
        decision["summary"] = f"Published {repo_ref} after successful probe."
        decision["next_action"] = "monitor repository adoption"


def append_review_log(config: dict, previous_status: str, new_status: str, what_changed: str, lessons: str) -> None:
    review_log = config.get("review_log")
    if not isinstance(review_log, list):
        review_log = []
        config["review_log"] = review_log
    review_log.append(
        {
            "date": iso_now(),
            "previous_status": previous_status,
            "new_status": new_status,
            "what_changed": what_changed,
            "lessons": lessons,
        }
    )


def update_pool_row(pool_file: Path, config: dict, case_dir: Path) -> None:
    hotspot_id = require_string(config, "meta", "hotspot_id")
    hotspot_name = require_string(config, "meta", "hotspot_name")
    owner = require_string(config, "meta", "owner")
    status = require_string(config, "decision", "final_status")
    shape = require_string(config, "gate_2_project_shape", "project_shape")
    strategy = require_string(config, "gate_3_repo_routing", "repo_strategy")
    last_review = (
        require_string(config, "execution_state", "publish", "last_run_at")
        or require_string(config, "execution_state", "probe", "last_run_at")
        or require_string(config, "execution_state", "discovery", "last_run_at")
        or require_string(config, "meta", "date")
    )
    next_review = require_string(config, "decision", "next_review_date")
    thesis = require_string(config, "decision", "one_line_thesis")
    try:
        case_display = case_dir.relative_to(ROOT).as_posix()
    except ValueError:
        case_display = str(case_dir)
    if "T" in last_review:
        last_review = last_review.split("T", 1)[0]
    new_row = (
        f"| {hotspot_id} | {hotspot_name} | {owner} | {status} | {shape} | "
        f"{strategy} | {last_review} | {next_review} | `{case_display}` | {thesis} |"
    )
    if pool_file.exists():
        lines = pool_file.read_text(encoding="utf-8").splitlines()
    else:
        lines = [
            "# Pipeline Pool",
            "",
            "| ID | Hotspot | Owner | Status | Project Shape | Repo Strategy | Last Review | Next Review | Case Folder | One-line Note |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
    updated = False
    for index, line in enumerate(lines):
        if line.startswith(f"| {hotspot_id} |"):
            lines[index] = new_row
            updated = True
            break
    if not updated:
        lines.append(new_row)
    pool_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def writeback_discovery(gate_file: Path, config: dict, case_dir: Path, pool_file: Path) -> None:
    previous_status = require_string(config, "decision", "final_status")
    append_review_log(
        config,
        previous_status=previous_status,
        new_status=previous_status,
        what_changed=f"discovered {config.get('execution_state', {}).get('discovery', {}).get('discovered_count', 0)} candidate repos",
        lessons=require_string(config, "gate_3_repo_routing", "recommended_reason"),
    )
    dump_gate_file(gate_file, config)
    update_pool_row(pool_file, config, case_dir)
    candidate = candidate_repo(config, 0) if config.get("gate_3_repo_routing", {}).get("candidate_repos") else {}
    update_memo_file(case_dir, config, candidate, mode="discover")
    update_review_checkpoint_file(case_dir, config, candidate)


def writeback_probe(gate_file: Path, config: dict, case_dir: Path, pool_file: Path, workspace: Path, candidate: dict, results: dict, mode: str, repo_ref: str = "") -> None:
    previous_status = require_string(config, "decision", "final_status")
    update_gate_after_probe(config, workspace, candidate, results, mode, repo_ref)
    new_status = require_string(config, "decision", "final_status")
    append_review_log(
        config,
        previous_status=previous_status,
        new_status=new_status,
        what_changed=f"{mode} executed in workspace {workspace}",
        lessons=f"install/build/test -> {command_status(results.get('install', {}))}/{command_status(results.get('build', {}))}/{command_status(results.get('test', {}))}",
    )
    dump_gate_file(gate_file, config)
    update_probe_run_file(case_dir, config, workspace, candidate, results, mode)
    update_pool_row(pool_file, config, case_dir)
    update_memo_file(case_dir, config, candidate, results, mode)
    update_review_checkpoint_file(case_dir, config, candidate)


def main() -> int:
    args = parse_args()
    gate_file, config = load_gate_file(args)
    case_dir = case_dir_from_gate(gate_file)
    pool_file = Path(args.pool_file).expanduser().resolve()
    workspace_root = Path(args.workspace_root).expanduser().resolve()
    workspace = workspace_dir(config, workspace_root)
    candidate = candidate_repo(config, args.candidate_index)
    print_plan(config, gate_file, workspace, candidate)

    if args.mode == "inspect":
        return 0

    if args.mode == "discover":
        print_section("Discovery")
        query = discovery_query(config, args.discover_query)
        candidates, strategy, reason = discover_candidates(config, query, args.discover_limit)
        print(f"Query: {query}")
        print(f"Discovered: {len(candidates)}")
        print(f"Recommended strategy: {strategy}")
        print(f"Reason: {reason}")
        for index, item in enumerate(candidates):
            print(f"[{index}] {item.get('name', '')} stars={item.get('stars', 0)} url={item.get('url', '')}")
        if args.execute and not args.no_writeback:
            writeback_discovery(gate_file, config, case_dir, pool_file)
        return 0

    print_section("Workspace Actions")
    actions = prepare_workspace(config, workspace, candidate, args.execute)
    for action in actions:
        print(action)

    print_section("Probe Actions")
    probe_results = run_probe(config, workspace, args.execute)
    for step, result in probe_results.items():
        print(f"{step}: {command_status(result)}")

    repo_ref = ""
    if args.mode == "publish":
        print_section("Publish Actions")
        publish_actions, repo_ref = publish_workspace(config, workspace, candidate, args.execute, args.allow_publish)
        for action in publish_actions:
            print(action)

    if args.execute and not args.no_writeback:
        writeback_probe(
            gate_file=gate_file,
            config=config,
            case_dir=case_dir,
            pool_file=pool_file,
            workspace=workspace,
            candidate=candidate,
            results=probe_results,
            mode=args.mode,
            repo_ref=repo_ref,
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PipelineError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
