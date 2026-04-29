"""Newsletter drafting via ``LLMClient.chat_json``.

``draft_newsletter`` is the main entry point — produces a newsletter dict
either by deriving from an existing article draft or starting from scratch.

The LLM call uses prompt family ``email_newsletter``. Mock mode returns the
deterministic fixture at ``agentflow/shared/mocks/email_newsletter.json``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agentflow.agent_email.storage import (
    make_newsletter_id,
    save_newsletter,
)
from agentflow.shared.bootstrap import agentflow_home
from agentflow.shared.llm_client import LLMClient
from agentflow.shared.logger import get_logger

_log = get_logger("agent_email.drafter")

_PROMPT_FAMILY = "email_newsletter"
_PROMPT_FAMILY_EDIT = "email_newsletter_edit"


def _prompt_path() -> Path:
    """Locate ``backend/prompts/email_newsletter.md``."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "prompts" / "email_newsletter.md"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("prompts/email_newsletter.md not found")


def _load_prompt_template() -> str:
    try:
        return _prompt_path().read_text(encoding="utf-8")
    except FileNotFoundError:
        # Reasonable fallback so mock runs don't fail in slim envs.
        return (
            "Turn the following blog draft into a short newsletter email.\n"
            "Rules: subject <= 45 chars, 3 sections (intro/body/closing), "
            "<= 2 inline images, include {unsubscribe_link} placeholder.\n\n"
            "blog draft: {draft_markdown}\n"
            "published_urls: {published_urls_if_any}\n"
            "user_handle: {user_handle}\n"
        )


def _load_draft(article_id: str) -> dict[str, Any]:
    draft_dir = agentflow_home() / "drafts" / article_id
    md_path = draft_dir / "draft.md"
    meta_path = draft_dir / "metadata.json"
    if not meta_path.exists():
        raise FileNotFoundError(
            f"No draft metadata for article_id={article_id} at {meta_path}"
        )
    metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    draft_markdown = md_path.read_text(encoding="utf-8") if md_path.exists() else ""
    return {
        "article_id": article_id,
        "title": metadata.get("title", ""),
        "draft_markdown": draft_markdown,
        "metadata": metadata,
    }


def _read_published_urls(article_id: str) -> list[str]:
    """Look up successful publish records for this article."""
    from agentflow.agent_d4.storage import read_publish_history

    urls: list[str] = []
    for rec in read_publish_history(article_id):
        if rec.get("status") == "success" and rec.get("published_url"):
            urls.append(rec["published_url"])
    return urls


def _build_prompt(
    *,
    draft_markdown: str,
    published_urls: list[str],
    user_handle: str,
    from_scratch_title: str | None = None,
) -> str:
    """Materialize the prompt template with the four expected substitutions."""
    template = _load_prompt_template()

    body = draft_markdown or ""
    if from_scratch_title and not body:
        body = f"(no existing draft; newsletter topic: {from_scratch_title})"

    substitutions = {
        "{draft_markdown}": body[:8000],  # keep prompt bounded
        "{published_urls_if_any}": ", ".join(published_urls) if published_urls else "(none yet)",
        "{user_handle}": user_handle or "(anonymous)",
        "{from_scratch_title}": from_scratch_title or "",
    }
    prompt = template
    for key, value in substitutions.items():
        prompt = prompt.replace(key, value)
    return prompt


async def draft_newsletter(
    article_id: str | None = None,
    from_scratch_title: str | None = None,
    *,
    user_handle: str | None = None,
) -> dict[str, Any]:
    """Produce a new newsletter draft. Persists it under ``~/.agentflow/newsletters/``.

    Exactly one of ``article_id`` / ``from_scratch_title`` should be provided.

    Returns a dict with keys:
    ``newsletter_id, subject, preview_text, html_body, plain_text_body, images_used``.
    """
    if not article_id and not from_scratch_title:
        raise ValueError("draft_newsletter requires article_id or from_scratch_title")

    published_urls: list[str] = []
    draft_markdown = ""
    article_title = ""
    if article_id:
        loaded = _load_draft(article_id)
        draft_markdown = loaded["draft_markdown"]
        article_title = loaded["title"]
        published_urls = _read_published_urls(article_id)

    import os

    handle = user_handle or os.environ.get("NEWSLETTER_FROM_NAME") or "agentflow"
    prompt = _build_prompt(
        draft_markdown=draft_markdown,
        published_urls=published_urls,
        user_handle=handle,
        from_scratch_title=from_scratch_title,
    )

    client = LLMClient()
    result = await client.chat_json(
        prompt_family=_PROMPT_FAMILY,
        prompt=prompt,
        max_tokens=2500,
    )

    subject = str(result.get("subject") or "").strip() or (article_title or from_scratch_title or "Newsletter")
    preview_text = str(result.get("preview_text") or "").strip()
    html_body = str(result.get("html_body") or "").strip()
    plain_text_body = str(result.get("plain_text_body") or "").strip()
    images_used = list(result.get("images_used") or [])

    # Enforce soft subject length — log a warning but don't reject.
    if len(subject) > 80:
        _log.warning("newsletter subject exceeds 80 chars (got %d)", len(subject))

    # If the LLM forgot the unsubscribe placeholder, splice it in so the
    # publisher's template substitution still works downstream.
    if "{unsubscribe_link}" not in html_body:
        html_body = html_body.rstrip() + '\n<p style="color:#6a737d;font-size:12px;margin-top:24px">Unsubscribe: <a href="{unsubscribe_link}">{unsubscribe_link}</a></p>'
    if "{unsubscribe_link}" not in plain_text_body:
        plain_text_body = plain_text_body.rstrip() + "\n\n—\nUnsubscribe: {unsubscribe_link}\n"

    # Substitute the article URL if we have one, so mock fixtures don't leak
    # their placeholder into the saved file.
    if published_urls:
        article_url = published_urls[0]
        html_body = html_body.replace("{article_url}", article_url)
        plain_text_body = plain_text_body.replace("{article_url}", article_url)
        html_body = html_body.replace("{article_title}", article_title or article_url)
        plain_text_body = plain_text_body.replace("{article_title}", article_title or article_url)

    newsletter_id = make_newsletter_id(seed=article_id or from_scratch_title)
    save_newsletter(
        newsletter_id=newsletter_id,
        subject=subject,
        preview_text=preview_text,
        html_body=html_body,
        plain_text_body=plain_text_body,
        article_id=article_id,
        images_used=images_used,
        status="draft",
    )

    return {
        "newsletter_id": newsletter_id,
        "subject": subject,
        "preview_text": preview_text,
        "html_body": html_body,
        "plain_text_body": plain_text_body,
        "images_used": images_used,
    }


# ---------------------------------------------------------------------------
# Edit
# ---------------------------------------------------------------------------


_EDITABLE_SECTIONS = {"subject", "preview_text", "intro", "body", "closing"}


async def edit_section(
    newsletter_id: str,
    section: str,
    command: str,
) -> dict[str, Any]:
    """LLM-powered edit on one of {subject, preview_text, intro, body, closing}.

    Loads the current newsletter, sends the existing text + user command, gets
    back an updated fragment, and rewrites the appropriate files on disk.
    """
    from agentflow.agent_email.storage import load_newsletter, save_newsletter

    if section not in _EDITABLE_SECTIONS:
        raise ValueError(
            f"unknown section {section!r}; must be one of {sorted(_EDITABLE_SECTIONS)}"
        )

    data = load_newsletter(newsletter_id)
    current = {
        "subject": data.get("subject", ""),
        "preview_text": data.get("preview_text", ""),
        "html_body": data.get("html_body", ""),
        "plain_text_body": data.get("plain_text_body", ""),
    }

    prompt = (
        "You are editing one piece of an email newsletter.\n\n"
        f"Section to edit: {section}\n"
        f"User command: {command}\n\n"
        f"Current subject: {current['subject']}\n"
        f"Current preview_text: {current['preview_text']}\n\n"
        "Current html_body (trimmed):\n"
        f"{current['html_body'][:4000]}\n\n"
        "Current plain_text_body (trimmed):\n"
        f"{current['plain_text_body'][:4000]}\n\n"
        "Respond as JSON: {\"section\": \"<section>\", \"updated_text\": \"...\", \"notes\": \"...\"}\n"
        "If section == 'intro'/'body'/'closing', updated_text should be a SHORT replacement for that paragraph.\n"
        "Preserve the {unsubscribe_link} placeholder.\n"
    )

    client = LLMClient()
    result = await client.chat_json(
        prompt_family=_PROMPT_FAMILY_EDIT,
        prompt=prompt,
        max_tokens=1500,
    )
    updated_text = str(result.get("updated_text") or "").strip()
    if not updated_text:
        raise ValueError("LLM returned empty updated_text")

    # Merge the fragment back. For intro/body/closing we do a best-effort
    # paragraph swap: newest replacement is inserted near the top / middle /
    # bottom of the existing body. This is intentionally simple in v0.1 —
    # users can always re-draft from scratch with `newsletter-draft` if the
    # edit makes a mess.
    new_subject = current["subject"]
    new_preview = current["preview_text"]
    new_html = current["html_body"]
    new_plain = current["plain_text_body"]

    if section == "subject":
        new_subject = updated_text
    elif section == "preview_text":
        new_preview = updated_text
    else:
        # Append the updated section at the end of the body (before the
        # unsubscribe footer) to avoid lossy regex surgery on HTML.
        marker = "{unsubscribe_link}"
        wrapped_html = f"<p><em>[{section} — edited]</em> {updated_text}</p>"
        wrapped_plain = f"\n[{section} — edited]\n{updated_text}\n"
        if marker in new_html:
            new_html = new_html.replace(marker, wrapped_html + marker, 1)
        else:
            new_html = new_html.rstrip() + "\n" + wrapped_html
        if marker in new_plain:
            new_plain = new_plain.replace(marker, wrapped_plain + marker, 1)
        else:
            new_plain = new_plain.rstrip() + wrapped_plain

    save_newsletter(
        newsletter_id=newsletter_id,
        subject=new_subject,
        preview_text=new_preview,
        html_body=new_html,
        plain_text_body=new_plain,
        article_id=data.get("article_id"),
        images_used=data.get("images_used"),
        status=data.get("status", "draft"),
    )

    return {
        "newsletter_id": newsletter_id,
        "section": section,
        "updated_text": updated_text,
        "notes": str(result.get("notes") or ""),
    }
