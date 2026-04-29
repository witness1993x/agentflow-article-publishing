"""`af learning-review` — weekly learning health report."""

from __future__ import annotations

import json as _json
import os
from typing import Any

import click

from agentflow.cli.commands import cli
from agentflow.shared.learning_review import build_learning_review


def _emit_json(obj: Any) -> None:
    click.echo(_json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def render_learning_review_markdown(report: dict[str, Any]) -> str:
    suggestions = report["suggestions"]
    publish = report["publish_history"]
    memory = report["memory_events"]
    style = report["style_learning"]
    lines = [
        f"# AgentFlow Weekly Learning Review ({report['since']})",
        "",
        "## Constraint Suggestions",
        (
            f"- pending={suggestions['counts']['pending']} "
            f"applied={suggestions['counts']['applied']} "
            f"dismissed={suggestions['counts']['dismissed']} "
            f"other={suggestions['counts']['other']}"
        ),
    ]
    if suggestions["top_items"]:
        lines.append("- top items:")
        for item in suggestions["top_items"][:5]:
            lines.append(
                f"  - [{item.get('status')}] {item.get('id')}: "
                f"{item.get('title') or '(untitled)'}"
            )
    else:
        lines.append("- no suggestions yet")

    lines.extend(
        [
            "",
            "## Publish History",
            (
                f"- success={publish['counts']['success']} "
                f"manual={publish['counts']['manual']} "
                f"failed={publish['counts']['failed']} "
                f"rolled_back={publish['counts']['rolled_back']} "
                f"rollback_failed={publish['counts']['rollback_failed']}"
            ),
        ]
    )
    if publish["per_platform"]:
        lines.append("- platform distribution:")
        for platform, counts in publish["per_platform"].items():
            lines.append(
                f"  - {platform}: success={counts['success']} manual={counts['manual']} "
                f"failed={counts['failed']} rolled_back={counts['rolled_back']} "
                f"total={counts['total']}"
            )
    if publish["recent_articles"]:
        lines.append("- recent articles:")
        for item in publish["recent_articles"][:5]:
            lines.append(
                f"  - {item.get('article_id')} / {item.get('platform')} / "
                f"{item.get('status')} / {item.get('published_at')}"
            )

    lines.extend(["", "## Memory Events"])
    for event_type, count in memory["counts"].items():
        lines.append(f"- {event_type}: {count}")

    lines.extend(
        [
            "",
            "## Style Learning",
            f"- style_corpus_count: {style['style_corpus_count']}",
            f"- style_profile_exists: {style['style_profile_exists']}",
            (
                "- recommend `af learn-style --from-published`: "
                f"{style['recommend_learn_style_from_published']}"
            ),
            "",
            "## Next Steps",
        ]
    )
    for rec in report["recommendations"]:
        lines.append(f"- {rec}")
    return "\n".join(lines)


def _post_to_tg(markdown: str) -> None:
    if not os.environ.get("TELEGRAM_BOT_TOKEN", "").strip():
        raise click.ClickException("TG is not configured: TELEGRAM_BOT_TOKEN is not set.")
    from agentflow.agent_review import daemon, tg_client

    chat_id = daemon.get_review_chat_id()
    if chat_id is None:
        raise click.ClickException(
            "TG is not configured: set TELEGRAM_REVIEW_CHAT_ID or run the review bot /start flow."
        )
    tg_client.send_long_text(chat_id, markdown, parse_mode=None)


@cli.command(
    "learning-review",
    help="Weekly learning review for suggestions, publish history, memory, and style state.",
)
@click.option(
    "--since",
    type=str,
    default="7d",
    show_default=True,
    help="Review window for history/memory: Nd (e.g. 7d) or 'all'.",
)
@click.option("--json", "as_json", is_flag=True, default=False)
@click.option(
    "--post-tg",
    is_flag=True,
    default=False,
    help="Post the Markdown report to the configured Telegram review chat.",
)
def learning_review(since: str, as_json: bool, post_tg: bool) -> None:
    try:
        report = build_learning_review(since=since)
    except ValueError as err:
        raise click.UsageError(str(err)) from err

    markdown = render_learning_review_markdown(report)
    if post_tg:
        _post_to_tg(markdown)

    if as_json:
        _emit_json(report)
        return
    click.echo(markdown)
