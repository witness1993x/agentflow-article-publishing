"""TopicIntent CLI helpers. Registered lazily by commands.py's import hook.

Currently exposes:

- ``af intent-check <article_id>`` — score how well a finished article
  reflects the current TopicIntent. Useful from Step 1b of publish, or from
  scripts that want to fail-fast before fan-out.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import click

from agentflow.cli.commands import _emit_json, cli
from agentflow.shared.bootstrap import agentflow_home


_WORD_RE = re.compile(r"[\w一-鿿]+", re.UNICODE)


def _tokenize(text: str) -> list[str]:
    """Lowercase word tokens + CJK 2-grams. Drops tokens shorter than 2 chars."""
    if not text:
        return []
    lowered = text.lower()
    tokens: list[str] = []
    for m in _WORD_RE.findall(lowered):
        if len(m) >= 2:
            tokens.append(m)
        # CJK run → add overlapping 2-grams so '量子' survives even if
        # surrounding text is '量子纠缠'.
        if any("一" <= ch <= "鿿" for ch in m):
            for i in range(len(m) - 1):
                bigram = m[i : i + 2]
                if len(bigram) == 2:
                    tokens.append(bigram)
    # De-dupe while preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _load_article_signal(article_id: str) -> tuple[str, dict[str, Any]]:
    """Return (signal_text, metadata) for the article. Raises ClickException."""
    draft_dir = agentflow_home() / "drafts" / article_id
    meta_path = draft_dir / "metadata.json"
    if not meta_path.exists():
        raise click.ClickException(f"no draft metadata at {meta_path}")
    try:
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"malformed metadata.json: {exc}")

    title = metadata.get("title") or ""
    sections = metadata.get("sections") or []
    first_heading = ""
    first_body = ""
    if sections and isinstance(sections, list):
        first = sections[0] if isinstance(sections[0], dict) else {}
        first_heading = first.get("heading") or ""
        first_body = first.get("content_markdown") or ""

    opening = metadata.get("opening") or ""
    central_claim = opening or first_body[:500]

    signal = "\n".join([title, first_heading, central_claim])
    return signal, metadata


@cli.command(
    "intent-check",
    help="Score how well a finished article reflects the current TopicIntent. "
    "Outputs alignment_score in 0..1 (simple keyword overlap).",
)
@click.argument("article_id")
@click.option("--json", "as_json", is_flag=True, default=False)
def intent_check(article_id: str, as_json: bool) -> None:
    from agentflow.shared.memory import intent_query_text, load_current_intent

    intent = load_current_intent()
    query = intent_query_text(intent)
    signal, metadata = _load_article_signal(article_id)

    if not query:
        result: dict[str, Any] = {
            "article_id": article_id,
            "intent": None,
            "alignment_score": None,
            "matched_tokens": [],
            "missing_tokens": [],
            "note": "no current intent set",
        }
        if as_json:
            _emit_json(result)
            return
        click.echo("no current intent set — nothing to check against.")
        click.echo("(run `af intent-set \"...\"` first, or ignore this check)")
        return

    tokens = _tokenize(query)
    signal_lower = signal.lower()
    matched = [t for t in tokens if t in signal_lower]
    missing = [t for t in tokens if t not in signal_lower]
    score = (len(matched) / len(tokens)) if tokens else 0.0

    result = {
        "article_id": article_id,
        "intent": query,
        "alignment_score": round(score, 3),
        "matched_tokens": matched,
        "missing_tokens": missing,
        "title": metadata.get("title"),
    }

    if as_json:
        _emit_json(result)
        return

    click.echo(f"article : {article_id}")
    click.echo(f"title   : {metadata.get('title', '(untitled)')}")
    click.echo(f"intent  : {query!r}")
    click.echo(
        f"score   : {score:.2f}  ({len(matched)}/{len(tokens)} intent tokens in article)"
    )
    if matched:
        click.echo(f"matched : {', '.join(matched)}")
    if missing:
        click.echo(f"missing : {', '.join(missing)}")
    if score < 0.1:
        click.echo("⚠ intent_drift: article does not obviously reflect the intent.")
    elif score < 0.5:
        click.echo("~ partial match: double-check alignment before publishing.")
    else:
        click.echo("ok: article aligns with current intent.")


# ---------------------------------------------------------------------------
# af publisher-show — view publisher_account for a topic profile or article
# ---------------------------------------------------------------------------


@cli.command(
    "publisher-show",
    help="Show publisher_account for a topic profile (default: from active intent) or article.",
)
@click.option(
    "--profile",
    "profile_id",
    type=str,
    default=None,
    help="Topic profile id to inspect (e.g. one of `af intent-show`'s profiles). Default: active intent's profile.",
)
@click.option(
    "--article",
    "article_id",
    type=str,
    default=None,
    help="Article id to inspect; reads its persisted publisher_account snapshot.",
)
@click.option("--json", "as_json", is_flag=True, default=False)
def publisher_show(
    profile_id: str | None,
    article_id: str | None,
    as_json: bool,
) -> None:
    from agentflow.shared.memory import load_current_intent
    from agentflow.shared.topic_profiles import (
        get_topic_profile,
        topic_profile_publisher_account,
        TopicProfileNotFoundError,
    )

    publisher: dict[str, Any] = {}
    source = ""

    if article_id:
        path = agentflow_home() / "drafts" / article_id / "metadata.json"
        if not path.exists():
            raise click.ClickException(f"no metadata.json for article {article_id!r}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as err:
            raise click.ClickException(f"could not read metadata.json: {err}")
        publisher = data.get("publisher_account") or {}
        source = f"article={article_id}"
    else:
        if profile_id is None:
            intent = load_current_intent() or {}
            profile_id = ((intent.get("profile") or {}).get("id") or "").strip() or None
        if not profile_id:
            raise click.ClickException(
                "no profile id; pass --profile <id> or --article <id>, or set an intent first"
            )
        try:
            profile = get_topic_profile(profile_id)
        except TopicProfileNotFoundError as err:
            raise click.ClickException(str(err))
        publisher = topic_profile_publisher_account(profile)
        source = f"profile={profile_id}"

    if as_json:
        _emit_json({"source": source, "publisher_account": publisher})
        return

    click.echo(f"source: {source}")
    if not publisher:
        click.echo("publisher_account: (none — no voice constraint configured)")
        return
    click.echo(f"brand:    {publisher.get('brand', '(unset)')}")
    click.echo(f"voice:    {publisher.get('voice', '(unset)')}")
    click.echo(f"pronoun:  {publisher.get('pronoun', '(unset)')}")
    do_list = publisher.get("do") or []
    dont_list = publisher.get("dont") or []
    facts = publisher.get("product_facts") or []
    tags = publisher.get("default_tags") or []
    if do_list:
        click.echo("do:")
        for d in do_list:
            click.echo(f"  - {d}")
    if dont_list:
        click.echo("dont:")
        for d in dont_list:
            click.echo(f"  - {d}")
    if facts:
        click.echo("product_facts:")
        for f in facts:
            click.echo(f"  - {f}")
    if tags:
        click.echo(f"default_tags: {tags}")
    if publisher.get("canonical_domain"):
        click.echo(f"canonical_domain: {publisher['canonical_domain']}")
