#!/usr/bin/env python3
"""Scaffold a new Window Gate opportunity workspace from templates."""

from __future__ import annotations

import argparse
import re
from datetime import date, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parent
TEMPLATES_DIR = ROOT / "templates"
DEFAULT_POOL_FILE = ROOT / "opportunity-pool.md"


def slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    return value or "opportunity"


def replace_once(text: str, old: str, new: str) -> str:
    if old not in text:
        raise ValueError(f"Expected template marker not found: {old!r}")
    return text.replace(old, new, 1)


def render_intake(
    template: str,
    opportunity_id: str,
    name: str,
    owner: str,
    today: str,
    question: str,
) -> str:
    text = template
    text = replace_once(text, "- Opportunity ID:", f"- Opportunity ID: {opportunity_id}")
    text = replace_once(text, "- Opportunity name:", f"- Opportunity name: {name}")
    text = replace_once(text, "- Date:", f"- Date: {today}")
    text = replace_once(text, "- Owner:", f"- Owner: {owner}")
    text = replace_once(text, "- One-line idea:", f"- One-line idea: {question}")
    return text


def render_gate_yaml(
    template: str,
    opportunity_id: str,
    name: str,
    owner: str,
    today: str,
    question: str,
    status: str,
    timing: str,
    next_review_date: str,
) -> str:
    text = template
    text = replace_once(text, '  opportunity_id: ""', f'  opportunity_id: "{opportunity_id}"')
    text = replace_once(text, '  opportunity_name: ""', f'  opportunity_name: "{name}"')
    text = replace_once(text, '  owner: ""', f'  owner: "{owner}"')
    text = replace_once(text, '  date: ""', f'  date: "{today}"')
    text = replace_once(text, '  status: "draft" # draft | watch | probe | build | drop', f'  status: "{status}" # draft | watch | probe | build | drop')
    text = replace_once(text, '  original_question: ""', f'  original_question: "{question}"')
    text = replace_once(text, '  final_status: "watch" # watch | probe | build | drop', f'  final_status: "{status}" # watch | probe | build | drop')
    text = replace_once(text, '  timing_verdict: "unknown" # unknown | too_early | now | late', f'  timing_verdict: "{timing}" # unknown | too_early | now | late')
    if next_review_date:
        text = replace_once(text, '  next_review_date: ""', f'  next_review_date: "{next_review_date}"')
    return text


def render_memo(
    template: str,
    opportunity_id: str,
    name: str,
    question: str,
    status: str,
    timing: str,
    thesis: str,
    next_review_date: str,
) -> str:
    text = template
    text = replace_once(text, "`Decision`: [watch / probe / build / drop]", f"`Decision`: `{status}`")
    if thesis:
        text = replace_once(text, "`One-line thesis`: [一句话写清楚为什么]", f"`One-line thesis`: {thesis}")
    text = replace_once(text, "`Timing verdict`: [unknown / too_early / now / late]", f"`Timing verdict`: `{timing}`")
    text = replace_once(text, "- Opportunity ID:", f"- Opportunity ID: {opportunity_id}")
    text = replace_once(text, "- Opportunity:", f"- Opportunity: {name}")
    text = replace_once(text, "- Original question:", f"- Original question: {question}")
    if next_review_date:
        text = replace_once(text, "`Next review date`: ", f"`Next review date`: {next_review_date}")
    return text


def render_probe(
    template: str,
    opportunity_id: str,
    name: str,
    owner: str,
    today: str,
) -> str:
    text = template
    text = replace_once(text, "- Opportunity ID:", f"- Opportunity ID: {opportunity_id}")
    text = replace_once(text, "- Opportunity:", f"- Opportunity: {name}")
    text = replace_once(text, "- Date:", f"- Date: {today}")
    text = replace_once(text, "- Owner:", f"- Owner: {owner}")
    return text


def render_review(
    template: str,
    opportunity_id: str,
    name: str,
    owner: str,
    status: str,
) -> str:
    text = template
    text = replace_once(text, "- Opportunity ID:", f"- Opportunity ID: {opportunity_id}")
    text = replace_once(text, "- Opportunity:", f"- Opportunity: {name}")
    text = replace_once(text, "- Reviewer:", f"- Reviewer: {owner}")
    text = replace_once(text, "- Previous status:", f"- Previous status: {status}")
    text = replace_once(text, "- New status:", f"- New status: {status}")
    return text


def build_case_files(
    case_dir: Path,
    opportunity_id: str,
    name: str,
    owner: str,
    today: str,
    question: str,
    status: str,
    timing: str,
    thesis: str,
    next_review_date: str,
) -> None:
    intake_template = (TEMPLATES_DIR / "opportunity-intake.template.md").read_text(encoding="utf-8")
    gate_template = (TEMPLATES_DIR / "window-gate.template.yaml").read_text(encoding="utf-8")
    memo_template = (TEMPLATES_DIR / "decision-memo.template.md").read_text(encoding="utf-8")
    probe_template = (TEMPLATES_DIR / "probe-run.template.md").read_text(encoding="utf-8")
    review_template = (TEMPLATES_DIR / "review-checkpoint.template.md").read_text(encoding="utf-8")

    files = {
        "01-opportunity-intake.md": render_intake(
            intake_template, opportunity_id, name, owner, today, question
        ),
        "02-window-gate.yaml": render_gate_yaml(
            gate_template,
            opportunity_id,
            name,
            owner,
            today,
            question,
            status,
            timing,
            next_review_date,
        ),
        "03-decision-memo.md": render_memo(
            memo_template, opportunity_id, name, question, status, timing, thesis, next_review_date
        ),
        "04-probe-run.md": render_probe(probe_template, opportunity_id, name, owner, today),
        "05-review-checkpoint.md": render_review(
            review_template, opportunity_id, name, owner, status
        ),
    }

    case_dir.mkdir(parents=True, exist_ok=False)
    for filename, content in files.items():
        (case_dir / filename).write_text(content, encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a new Window Gate opportunity workspace."
    )
    parser.add_argument("--name", required=True, help="Opportunity name")
    parser.add_argument("--owner", default="founder", help="Owner or reviewer name")
    parser.add_argument(
        "--question",
        help='Original question to seed the templates. Default: "Should we pursue <name>?"',
    )
    parser.add_argument(
        "--slug",
        help="Optional output slug. Defaults to a slugified form of the name.",
    )
    parser.add_argument(
        "--output-dir",
        default=str(ROOT / "cases"),
        help="Directory where the case folder will be created.",
    )
    parser.add_argument(
        "--status",
        default="watch",
        choices=["draft", "watch", "probe", "build", "drop"],
        help="Initial status for the new opportunity.",
    )
    parser.add_argument(
        "--timing",
        default="unknown",
        choices=["unknown", "too_early", "now", "late"],
        help="Initial timing verdict.",
    )
    parser.add_argument(
        "--thesis",
        default="",
        help="Optional one-line thesis to seed the decision memo and pool.",
    )
    parser.add_argument(
        "--next-review-date",
        default="",
        help="Optional next review date (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--review-days",
        type=int,
        default=14,
        help="Fallback review window in days when --next-review-date is omitted.",
    )
    parser.add_argument(
        "--pool-file",
        default=str(DEFAULT_POOL_FILE),
        help="Opportunity pool markdown file to update.",
    )
    parser.add_argument(
        "--skip-pool",
        action="store_true",
        help="Do not append the new opportunity to the pool file.",
    )
    return parser.parse_args()


def ensure_pool_file(pool_file: Path) -> None:
    if pool_file.exists():
        return
    template = (TEMPLATES_DIR / "opportunity-pool.template.md").read_text(encoding="utf-8")
    pool_file.parent.mkdir(parents=True, exist_ok=True)
    pool_file.write_text(template, encoding="utf-8")


def next_opportunity_id(pool_file: Path) -> str:
    ensure_pool_file(pool_file)
    text = pool_file.read_text(encoding="utf-8")
    numbers = [int(match.group(1)) for match in re.finditer(r"\bOPP-(\d{3,})\b", text)]
    next_number = (max(numbers) + 1) if numbers else 1
    return f"OPP-{next_number:03d}"


def append_pool_entry(
    pool_file: Path,
    opportunity_id: str,
    name: str,
    owner: str,
    status: str,
    timing: str,
    last_review: str,
    next_review_date: str,
    case_folder: str,
    thesis: str,
) -> None:
    ensure_pool_file(pool_file)
    row = (
        f"| {opportunity_id} | {name} | {owner} | {status} | {timing} | "
        f"{last_review} | {next_review_date} | `{case_folder}` | {thesis} |\n"
    )
    with pool_file.open("a", encoding="utf-8") as handle:
        handle.write(row)


def resolve_next_review_date(args: argparse.Namespace, today: date) -> str:
    if args.next_review_date:
        return args.next_review_date
    if args.status in {"watch", "probe", "build"}:
        return (today + timedelta(days=args.review_days)).isoformat()
    return ""


def display_case_folder(case_dir: Path) -> str:
    try:
        return case_dir.relative_to(ROOT).as_posix()
    except ValueError:
        return case_dir.as_posix()


def main() -> int:
    args = parse_args()
    today_obj = date.today()
    today = today_obj.isoformat()
    slug = args.slug or slugify(args.name)
    question = args.question or f"Should we pursue {args.name}?"
    next_review_date = resolve_next_review_date(args, today_obj)
    output_dir = Path(args.output_dir).expanduser().resolve()
    pool_file = Path(args.pool_file).expanduser().resolve()
    opportunity_id = next_opportunity_id(pool_file)
    case_dir = output_dir / f"{opportunity_id}-{today}-{slug}"

    build_case_files(
        case_dir,
        opportunity_id,
        args.name,
        args.owner,
        today,
        question,
        args.status,
        args.timing,
        args.thesis,
        next_review_date,
    )
    if not args.skip_pool:
        append_pool_entry(
            pool_file,
            opportunity_id,
            args.name,
            args.owner,
            args.status,
            args.timing,
            today,
            next_review_date,
            display_case_folder(case_dir),
            args.thesis,
        )
    print(case_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
