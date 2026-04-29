"""Newsletter CLI commands — registered onto the main ``af`` Click group.

All commands:

* Support ``--json`` for script/skill parsing (stdout only; human logs go stderr).
* Write a memory event to ``~/.agentflow/memory/events.jsonl`` on mutation.
* Respect ``MOCK_LLM=true`` — newsletter-send and notify never touch the network.
"""

from __future__ import annotations

import asyncio
import json as _json
import os
from datetime import datetime, timezone
from typing import Any

import click

from agentflow.cli.commands import cli


def _stderr(msg: str) -> None:
    click.echo(msg, err=True)


def _emit_json(obj: Any) -> None:
    click.echo(_json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# af newsletter-draft
# ---------------------------------------------------------------------------


@cli.command(
    "newsletter-draft",
    help="Derive a newsletter from an existing article draft or start from scratch.",
)
@click.argument("article_id", required=False)
@click.option(
    "--from-scratch",
    "from_scratch_title",
    type=str,
    default=None,
    help="Draft a newsletter without an existing article; pass a working title.",
)
@click.option("--json", "as_json", is_flag=True, default=False)
def newsletter_draft(
    article_id: str | None,
    from_scratch_title: str | None,
    as_json: bool,
) -> None:
    from agentflow.agent_email.drafter import draft_newsletter
    from agentflow.shared.memory import append_memory_event

    if not article_id and not from_scratch_title:
        raise click.UsageError(
            "Provide either <article_id> or --from-scratch 'title'."
        )
    if article_id and from_scratch_title:
        raise click.UsageError(
            "Pass either <article_id> OR --from-scratch, not both."
        )

    try:
        result = asyncio.run(
            draft_newsletter(
                article_id=article_id,
                from_scratch_title=from_scratch_title,
            )
        )
    except FileNotFoundError as err:
        raise click.ClickException(str(err))
    except ValueError as err:
        raise click.UsageError(str(err))

    append_memory_event(
        "newsletter_drafted",
        article_id=article_id,
        payload={
            "newsletter_id": result["newsletter_id"],
            "from_scratch_title": from_scratch_title,
            "subject": result["subject"],
            "subject_char_count": len(result["subject"]),
            "images_used": result.get("images_used") or [],
        },
    )

    if as_json:
        _emit_json(result)
        return

    click.echo(f"newsletter_id: {result['newsletter_id']}")
    click.echo(f"subject:       {result['subject']}  ({len(result['subject'])} chars)")
    click.echo(f"preview_text:  {result['preview_text']}")
    click.echo(
        f"body:          {len(result['html_body'])} html chars, "
        f"{len(result['plain_text_body'])} text chars"
    )
    click.echo(f"images:        {len(result.get('images_used') or [])}")


# ---------------------------------------------------------------------------
# af newsletter-show
# ---------------------------------------------------------------------------


@cli.command("newsletter-show", help="Show a newsletter draft by id.")
@click.argument("newsletter_id")
@click.option("--json", "as_json", is_flag=True, default=False)
def newsletter_show(newsletter_id: str, as_json: bool) -> None:
    from agentflow.agent_email.storage import load_newsletter

    try:
        data = load_newsletter(newsletter_id)
    except FileNotFoundError as err:
        raise click.ClickException(str(err))

    if as_json:
        _emit_json(data)
        return

    click.echo(f"newsletter_id: {data.get('newsletter_id')}")
    click.echo(f"article_id:    {data.get('article_id') or '-'}")
    click.echo(f"status:        {data.get('status', 'draft')}")
    click.echo(f"subject:       {data.get('subject')}")
    click.echo(f"preview_text:  {data.get('preview_text')}")
    click.echo(f"html_body:     {len(data.get('html_body') or '')} chars")
    click.echo(f"plain_text:    {len(data.get('plain_text_body') or '')} chars")
    click.echo(f"created_at:    {data.get('created_at')}")
    click.echo(f"updated_at:    {data.get('updated_at')}")
    click.echo("")
    click.echo("plain text body (first 40 lines):")
    for line in (data.get("plain_text_body") or "").splitlines()[:40]:
        click.echo(f"  {line}")


# ---------------------------------------------------------------------------
# af newsletter-edit
# ---------------------------------------------------------------------------


@cli.command(
    "newsletter-edit",
    help="Apply an LLM-powered edit to one section of a newsletter draft.",
)
@click.argument("newsletter_id")
@click.option(
    "--section",
    type=click.Choice(["subject", "preview_text", "intro", "body", "closing"]),
    required=True,
)
@click.option("--command", "edit_command", type=str, required=True)
@click.option("--json", "as_json", is_flag=True, default=False)
def newsletter_edit(
    newsletter_id: str,
    section: str,
    edit_command: str,
    as_json: bool,
) -> None:
    from agentflow.agent_email.drafter import edit_section
    from agentflow.shared.memory import append_memory_event

    try:
        result = asyncio.run(
            edit_section(newsletter_id, section=section, command=edit_command)
        )
    except FileNotFoundError as err:
        raise click.ClickException(str(err))
    except ValueError as err:
        raise click.UsageError(str(err))

    append_memory_event(
        "newsletter_edited",
        article_id=None,
        payload={
            "newsletter_id": newsletter_id,
            "section": section,
            "command": edit_command,
        },
    )

    if as_json:
        _emit_json(result)
        return
    click.echo(f"newsletter_id: {newsletter_id}")
    click.echo(f"section:       {section}")
    click.echo(f"command:       {edit_command}")
    click.echo(f"updated_text:  {result['updated_text']}")
    if result.get("notes"):
        click.echo(f"notes:         {result['notes']}")


# ---------------------------------------------------------------------------
# af newsletter-preview-send
# ---------------------------------------------------------------------------


def _build_platform_version(data: dict[str, Any], *, to: list[str]):
    from agentflow.shared.models import PlatformVersion

    return PlatformVersion(
        platform="email_newsletter",
        content=data.get("html_body") or "",
        metadata={
            "subject": data.get("subject", ""),
            "preview_text": data.get("preview_text", ""),
            "plain_text_body": data.get("plain_text_body", ""),
            "from_email": os.environ.get("NEWSLETTER_FROM_EMAIL"),
            "from_name": os.environ.get("NEWSLETTER_FROM_NAME"),
            "reply_to": os.environ.get("NEWSLETTER_REPLY_TO"),
            "to": to,
            # Preview sends never go through the audience — we want a 1:1 send.
            "audience_id": None,
        },
    )


def _prepare_audience_send(data: dict[str, Any]):
    from agentflow.agent_d4.publishers.email import EmailPublisher

    audience_id = os.environ.get("NEWSLETTER_AUDIENCE_ID")
    from_email = os.environ.get("NEWSLETTER_FROM_EMAIL")
    if not audience_id and not EmailPublisher._is_mock_mode():
        raise click.ClickException(
            "newsletter-send requires NEWSLETTER_AUDIENCE_ID (or run with MOCK_LLM=true)."
        )

    # When sending to audience, `to` falls back to from_email in the publisher.
    version = _build_platform_version(data, to=[from_email] if from_email else [])
    version.metadata["audience_id"] = audience_id
    return version, audience_id, from_email


def _append_newsletter_publish_history(
    data: dict[str, Any],
    newsletter_id: str,
    result,
) -> None:
    from agentflow.agent_d4.storage import append_publish_record

    article_id_for_history = data.get("article_id") or newsletter_id
    append_publish_record(article_id_for_history, result)


def _correction_subject(subject: str, correction_count: int) -> str:
    cleaned = (subject or "").strip()
    lowered = cleaned.lower()
    if lowered.startswith("correction:") or cleaned.startswith("更正"):
        return cleaned
    prefix = "更正：" if any("\u4e00" <= ch <= "\u9fff" for ch in cleaned) else "Correction: "
    if correction_count <= 0:
        return f"{prefix}{cleaned}" if cleaned else prefix.strip()
    return f"{prefix}{cleaned}" if cleaned else f"{prefix.strip()} #{correction_count + 1}"


@cli.command(
    "newsletter-preview-send",
    help="Send a test copy of a newsletter to yourself or an explicit email.",
)
@click.argument("newsletter_id")
@click.option(
    "--to",
    "to_addr",
    type=str,
    required=True,
    help="Recipient email, or 'self' (= NEWSLETTER_REPLY_TO).",
)
@click.option("--json", "as_json", is_flag=True, default=False)
def newsletter_preview_send(
    newsletter_id: str,
    to_addr: str,
    as_json: bool,
) -> None:
    from agentflow.agent_d4.publishers.email import EmailPublisher
    from agentflow.agent_email.storage import load_newsletter
    from agentflow.shared.memory import append_memory_event

    try:
        data = load_newsletter(newsletter_id)
    except FileNotFoundError as err:
        raise click.ClickException(str(err))

    target = to_addr
    if to_addr == "self":
        target = (
            os.environ.get("NEWSLETTER_REPLY_TO")
            or os.environ.get("NEWSLETTER_FROM_EMAIL")
            or ""
        )
        if not target:
            if EmailPublisher._is_mock_mode():
                target = "self@mock.local"
            else:
                raise click.ClickException(
                    "'--to self' requires NEWSLETTER_REPLY_TO in env."
                )

    if "@" not in target and not EmailPublisher._is_mock_mode():
        raise click.UsageError(f"'{target}' does not look like an email address.")

    version = _build_platform_version(data, to=[target])
    publisher = EmailPublisher(credentials={})
    result = asyncio.run(publisher.publish(version))

    append_memory_event(
        "newsletter_preview_sent",
        article_id=data.get("article_id"),
        payload={
            "newsletter_id": newsletter_id,
            "to": target,
            "status": result.status,
            "platform_post_id": result.platform_post_id,
            "failure_reason": result.failure_reason,
        },
    )

    payload = {
        "newsletter_id": newsletter_id,
        "to": target,
        "status": result.status,
        "platform_post_id": result.platform_post_id,
        "failure_reason": result.failure_reason,
    }
    if as_json:
        _emit_json(payload)
        return

    if result.status == "success":
        click.echo(f"preview sent to {target}  resend_id={result.platform_post_id}")
    else:
        raise click.ClickException(
            f"preview send failed: {result.failure_reason}"
        )


# ---------------------------------------------------------------------------
# af newsletter-send
# ---------------------------------------------------------------------------


@cli.command(
    "newsletter-send",
    help="Send the newsletter to the configured audience (Resend audience_id).",
)
@click.argument("newsletter_id")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Resolve audience + build payload, but don't actually hit Resend.",
)
@click.option("--json", "as_json", is_flag=True, default=False)
def newsletter_send(newsletter_id: str, dry_run: bool, as_json: bool) -> None:
    from agentflow.agent_d4.publishers.email import EmailPublisher
    from agentflow.agent_email.storage import load_newsletter, save_newsletter
    from agentflow.shared.memory import append_memory_event

    try:
        data = load_newsletter(newsletter_id)
    except FileNotFoundError as err:
        raise click.ClickException(str(err))

    version, audience_id, from_email = _prepare_audience_send(data)

    if dry_run:
        summary = {
            "newsletter_id": newsletter_id,
            "dry_run": True,
            "audience_id": audience_id,
            "from": from_email,
            "subject": data.get("subject"),
            "html_len": len(data.get("html_body") or ""),
            "plain_len": len(data.get("plain_text_body") or ""),
        }
        if as_json:
            _emit_json(summary)
            return
        click.echo(_json.dumps(summary, ensure_ascii=False, indent=2))
        return

    publisher = EmailPublisher(credentials={})
    result = asyncio.run(publisher.publish(version))

    # Persist to publish_history.jsonl so `af memory-tail` / report tools see it.
    _append_newsletter_publish_history(data, newsletter_id, result)

    # Update newsletter status.
    save_newsletter(
        newsletter_id=newsletter_id,
        subject=data.get("subject", ""),
        preview_text=data.get("preview_text", ""),
        html_body=data.get("html_body", ""),
        plain_text_body=data.get("plain_text_body", ""),
        article_id=data.get("article_id"),
        images_used=data.get("images_used"),
        status="sent" if result.status == "success" else "send_failed",
        extra={
            "last_platform_post_id": result.platform_post_id,
            "last_sent_at": _now_iso() if result.status == "success" else None,
            "last_failure_reason": result.failure_reason,
        },
    )

    append_memory_event(
        "newsletter_sent",
        article_id=data.get("article_id"),
        payload={
            "newsletter_id": newsletter_id,
            "audience_id": audience_id,
            "status": result.status,
            "platform_post_id": result.platform_post_id,
            "failure_reason": result.failure_reason,
        },
    )

    payload = {
        "newsletter_id": newsletter_id,
        "status": result.status,
        "platform_post_id": result.platform_post_id,
        "failure_reason": result.failure_reason,
        "audience_id": audience_id,
    }
    if as_json:
        _emit_json(payload)
        return

    if result.status == "success":
        click.echo(
            f"newsletter {newsletter_id} sent  resend_id={result.platform_post_id}"
        )
    else:
        raise click.ClickException(f"send failed: {result.failure_reason}")


# ---------------------------------------------------------------------------
# af newsletter-correction
# ---------------------------------------------------------------------------


@cli.command(
    "newsletter-correction",
    help="Send a follow-up correction email to the configured audience.",
)
@click.argument("newsletter_id")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Build the correction payload, but don't actually hit Resend.",
)
@click.option("--json", "as_json", is_flag=True, default=False)
def newsletter_correction(newsletter_id: str, dry_run: bool, as_json: bool) -> None:
    from agentflow.agent_d4.publishers.email import EmailPublisher
    from agentflow.agent_email.storage import load_newsletter, save_newsletter
    from agentflow.shared.memory import append_memory_event

    try:
        data = load_newsletter(newsletter_id)
    except FileNotFoundError as err:
        raise click.ClickException(str(err))

    if (
        data.get("status") != "sent"
        and not data.get("last_platform_post_id")
        and not data.get("last_correction_platform_post_id")
    ):
        raise click.ClickException(
            "newsletter-correction requires a previously sent newsletter."
        )

    correction_count = int(data.get("correction_count") or 0)
    version, audience_id, from_email = _prepare_audience_send(data)
    correction_of = (
        data.get("last_correction_platform_post_id") or data.get("last_platform_post_id")
    )
    correction_subject = _correction_subject(data.get("subject", ""), correction_count)
    version.metadata["subject"] = correction_subject

    if dry_run:
        summary = {
            "newsletter_id": newsletter_id,
            "dry_run": True,
            "audience_id": audience_id,
            "from": from_email,
            "subject": correction_subject,
            "correction_count": correction_count,
            "correction_of_platform_post_id": correction_of,
            "html_len": len(data.get("html_body") or ""),
            "plain_len": len(data.get("plain_text_body") or ""),
        }
        if as_json:
            _emit_json(summary)
            return
        click.echo(_json.dumps(summary, ensure_ascii=False, indent=2))
        return

    publisher = EmailPublisher(credentials={})
    result = asyncio.run(publisher.publish(version))
    _append_newsletter_publish_history(data, newsletter_id, result)

    save_newsletter(
        newsletter_id=newsletter_id,
        subject=data.get("subject", ""),
        preview_text=data.get("preview_text", ""),
        html_body=data.get("html_body", ""),
        plain_text_body=data.get("plain_text_body", ""),
        article_id=data.get("article_id"),
        images_used=data.get("images_used"),
        status=data.get("status", "draft"),
        extra={
            "correction_count": correction_count + (1 if result.status == "success" else 0),
            "last_correction_at": _now_iso() if result.status == "success" else None,
            "last_correction_platform_post_id": result.platform_post_id,
            "last_correction_failure_reason": result.failure_reason,
            "last_correction_of_platform_post_id": correction_of,
            "last_correction_subject": correction_subject,
        },
    )

    append_memory_event(
        "newsletter_correction_sent",
        article_id=data.get("article_id"),
        payload={
            "newsletter_id": newsletter_id,
            "audience_id": audience_id,
            "status": result.status,
            "platform_post_id": result.platform_post_id,
            "failure_reason": result.failure_reason,
            "correction_of_platform_post_id": correction_of,
            "correction_subject": correction_subject,
        },
    )

    payload = {
        "newsletter_id": newsletter_id,
        "status": result.status,
        "platform_post_id": result.platform_post_id,
        "failure_reason": result.failure_reason,
        "audience_id": audience_id,
        "correction_of_platform_post_id": correction_of,
        "correction_subject": correction_subject,
    }
    if as_json:
        _emit_json(payload)
        return

    if result.status == "success":
        click.echo(
            f"newsletter correction sent for {newsletter_id}  "
            f"resend_id={result.platform_post_id}"
        )
    else:
        raise click.ClickException(f"correction send failed: {result.failure_reason}")


# ---------------------------------------------------------------------------
# af newsletter-list-show
# ---------------------------------------------------------------------------


@cli.command(
    "newsletter-list-show",
    help="List every newsletter under ~/.agentflow/newsletters/.",
)
@click.option("--json", "as_json", is_flag=True, default=False)
def newsletter_list_show(as_json: bool) -> None:
    from agentflow.agent_email.storage import list_newsletters

    items = list_newsletters()
    if as_json:
        _emit_json({"newsletters": items, "count": len(items)})
        return

    if not items:
        click.echo("(no newsletters)")
        return

    click.echo(f"{'NEWSLETTER_ID':<40} {'STATUS':<12} {'SUBJECT':<40} UPDATED")
    click.echo(f"{'-' * 40} {'-' * 12} {'-' * 40} --------------------")
    for item in items:
        click.echo(
            f"{(item.get('newsletter_id') or ''):<40} "
            f"{(item.get('status') or 'draft'):<12} "
            f"{(item.get('subject') or '')[:40]:<40} "
            f"{item.get('updated_at') or '-'}"
        )


# ---------------------------------------------------------------------------
# af notify (system self-notification)
# ---------------------------------------------------------------------------


@cli.command(
    "notify",
    help="Send a short plain-text self-notification to NEWSLETTER_REPLY_TO.",
)
@click.argument("message")
@click.option(
    "--event",
    "event_type",
    type=str,
    default="system_notify",
    help="Event label stored in memory (e.g. hotspots_scan_complete).",
)
@click.option("--json", "as_json", is_flag=True, default=False)
def notify(message: str, event_type: str, as_json: bool) -> None:
    from agentflow.agent_d4.publishers.email import EmailPublisher
    from agentflow.shared.memory import append_memory_event
    from agentflow.shared.models import PlatformVersion

    to_addr = os.environ.get("NEWSLETTER_REPLY_TO") or os.environ.get(
        "NEWSLETTER_FROM_EMAIL"
    )
    from_email = os.environ.get("NEWSLETTER_FROM_EMAIL") or to_addr

    is_mock = EmailPublisher._is_mock_mode()
    if not is_mock and (not to_addr or not from_email):
        raise click.ClickException(
            "notify requires NEWSLETTER_REPLY_TO and NEWSLETTER_FROM_EMAIL "
            "(or run with MOCK_LLM=true to mock silently)."
        )

    subject = f"[agentflow] {event_type}"
    html_body = (
        f"<p>{message}</p><p style=\"color:#6a737d;font-size:12px\">"
        f"Sent at {_now_iso()} — event={event_type}</p>"
    )
    text_body = f"{message}\n\nSent at {_now_iso()} — event={event_type}\n"

    version = PlatformVersion(
        platform="email_newsletter",
        content=html_body,
        metadata={
            "subject": subject,
            "plain_text_body": text_body,
            "from_email": from_email,
            "from_name": os.environ.get("NEWSLETTER_FROM_NAME") or "agentflow",
            "reply_to": to_addr,
            "to": [to_addr] if to_addr else [],
            "audience_id": None,
        },
    )

    publisher = EmailPublisher(credentials={})
    result = asyncio.run(publisher.publish(version))

    append_memory_event(
        "system_notified",
        article_id=None,
        payload={
            "event_type": event_type,
            "message": message,
            "to": to_addr,
            "status": result.status,
            "platform_post_id": result.platform_post_id,
            "failure_reason": result.failure_reason,
        },
    )

    payload = {
        "event": event_type,
        "to": to_addr,
        "status": result.status,
        "platform_post_id": result.platform_post_id,
        "failure_reason": result.failure_reason,
        "mock": is_mock,
    }
    if as_json:
        _emit_json(payload)
        return

    if result.status == "success":
        click.echo(
            f"notified {to_addr or '(mock)'}  event={event_type}  "
            f"resend_id={result.platform_post_id}"
        )
    else:
        raise click.ClickException(f"notify failed: {result.failure_reason}")
