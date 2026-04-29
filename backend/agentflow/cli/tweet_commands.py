"""Twitter CLI subcommands. Registered lazily by commands.py's import hook."""

from __future__ import annotations

import asyncio
import json as _json
from datetime import datetime, timedelta, timezone

import click

import os

from agentflow.cli.commands import cli, _emit_json, _load_yaml_overrides


@cli.command(
    "tweet-draft",
    help="Draft a single tweet or thread from a hotspot or article.",
)
@click.argument("source_id", required=True)
@click.option(
    "--form",
    type=click.Choice(["single", "thread"]),
    default="single",
    show_default=True,
)
@click.option(
    "--from-article",
    "from_article",
    is_flag=True,
    default=False,
    help="Treat SOURCE_ID as an article_id instead of a hotspot_id.",
)
@click.option("--angle", "angle_index", type=int, default=None)
@click.option("--json", "as_json", is_flag=True, default=False)
@click.option(
    "--from-file",
    "from_file",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="YAML preset (thread_template.yaml schema) — surfaces tone / "
    "max_tweets / pin_first_tweet to the tweet drafter via env.",
)
def tweet_draft(
    source_id: str,
    form: str,
    from_article: bool,
    angle_index: int | None,
    as_json: bool,
    from_file: str | None,
) -> None:
    from agentflow.agent_tw.drafter import draft_tweet
    from agentflow.shared.memory import append_memory_event

    overrides = _load_yaml_overrides(from_file)
    if overrides:
        if form == "single" and overrides.get("thread_form"):
            form = str(overrides["thread_form"])
        for k in ("max_tweets", "tone", "pin_first_tweet"):
            if k in overrides and overrides[k] is not None:
                os.environ[f"AGENTFLOW_TWEET_{k.upper()}"] = (
                    _json.dumps(overrides[k]) if not isinstance(overrides[k], str) else overrides[k]
                )

    kwargs: dict = {"form": form, "angle_index": angle_index}
    if from_article:
        kwargs["article_id"] = source_id
    else:
        kwargs["hotspot_id"] = source_id

    try:
        payload = asyncio.run(draft_tweet(**kwargs))
    except FileNotFoundError as err:
        raise click.ClickException(str(err))
    except ValueError as err:
        raise click.UsageError(str(err))

    append_memory_event(
        "tweet_draft_created",
        article_id=payload["tweet_id"],
        hotspot_id=source_id if not from_article else None,
        payload={
            "form": payload["form"],
            "tweet_count": len(payload["tweets"]),
            "source_type": payload["source_type"],
            "source_id": payload["source_id"],
        },
    )

    if as_json:
        _emit_json(payload)
        return
    click.echo(f"tweet {payload['tweet_id']}: {payload['form']} "
               f"({len(payload['tweets'])} tweets)")
    click.echo(f"  hook: {payload.get('intended_hook', '')[:100]}")
    for t in payload["tweets"]:
        click.echo(
            f"  [{t['index']}] ({t['char_count']} ch)"
            f"{' [img:' + t['image_slot'] + ']' if t.get('image_slot') else ''}"
        )
        click.echo(f"      {t['text'][:120]}")


@cli.command("tweet-show", help="Show a tweet draft by id.")
@click.argument("tweet_id")
@click.option("--json", "as_json", is_flag=True, default=False)
def tweet_show(tweet_id: str, as_json: bool) -> None:
    from agentflow.agent_tw import storage

    try:
        data = storage.load(tweet_id)
    except FileNotFoundError:
        raise click.ClickException(f"tweet not found: {tweet_id}")

    if as_json:
        _emit_json(data)
        return
    click.echo(f"Tweet {tweet_id} ({data.get('form')}, status={data.get('status')})")
    click.echo(f"  source: {data.get('source_type')} / {data.get('source_id')}")
    click.echo(f"  hook:   {data.get('intended_hook', '')}")
    click.echo(f"  refs:   {data.get('source_refs') or []}")
    for t in data.get("tweets", []):
        click.echo(f"\n  [{t['index']}] ({t['char_count']} ch)")
        if t.get("image_slot"):
            click.echo(f"      [image: {t['image_slot']}] {t.get('image_hint') or ''}")
        click.echo(f"      {t['text']}")


@cli.command("tweet-edit", help="Apply an edit to a tweet draft.")
@click.argument("tweet_id")
@click.option("--index", "index", type=int, default=None,
              help="Target tweet index for --command.")
@click.option("--command", "command", default=None,
              help="Natural-language edit command (e.g. '改短', '去AI味').")
@click.option("--split", "split_at", type=int, default=None,
              help="Split tweet at index into two (thread grows by 1).")
@click.option("--merge", "merge_csv", default=None,
              help="Merge two adjacent indices, e.g. --merge 0,1 (thread shrinks by 1).")
@click.option("--reorder", "reorder_csv", default=None,
              help="Reorder thread, e.g. --reorder 0,3,1,2,4.")
@click.option("--json", "as_json", is_flag=True, default=False)
def tweet_edit(
    tweet_id: str,
    index: int | None,
    command: str | None,
    split_at: int | None,
    merge_csv: str | None,
    reorder_csv: str | None,
    as_json: bool,
) -> None:
    from agentflow.agent_tw.drafter import edit_tweet
    from agentflow.shared.memory import append_memory_event

    merge = None
    if merge_csv:
        parts = [int(x) for x in merge_csv.split(",")]
        if len(parts) != 2:
            raise click.UsageError("--merge wants exactly two comma-separated indices")
        merge = (parts[0], parts[1])

    reorder = None
    if reorder_csv:
        reorder = [int(x) for x in reorder_csv.split(",")]

    try:
        result = asyncio.run(
            edit_tweet(
                tweet_id,
                index=index,
                command=command,
                split_at=split_at,
                merge=merge,
                reorder=reorder,
            )
        )
    except ValueError as err:
        raise click.UsageError(str(err))
    except FileNotFoundError as err:
        raise click.ClickException(str(err))

    op = (
        "command" if command else
        "split" if split_at is not None else
        "merge" if merge else
        "reorder"
    )
    append_memory_event(
        "tweet_edited",
        article_id=tweet_id,
        payload={"op": op, "index": index, "command": command,
                 "split_at": split_at, "merge": merge, "reorder": reorder,
                 "tweet_count": len(result["tweets"])},
    )

    if as_json:
        _emit_json(result)
        return
    click.echo(f"edited {tweet_id} ({op}) — now {len(result['tweets'])} tweets")


@cli.command("tweet-publish", help="Publish a tweet draft (single or thread).")
@click.argument("tweet_id")
@click.option("--dry-run", "dry_run", is_flag=True, default=False)
@click.option("--json", "as_json", is_flag=True, default=False)
def tweet_publish(tweet_id: str, dry_run: bool, as_json: bool) -> None:
    from agentflow.agent_d4.publishers.twitter import TwitterPublisher
    from agentflow.agent_d4.storage import append_publish_record
    from agentflow.agent_tw import storage as tw_storage
    from agentflow.shared.memory import append_memory_event
    from agentflow.shared.models import PlatformVersion

    try:
        meta = tw_storage.load(tweet_id)
    except FileNotFoundError:
        raise click.ClickException(f"tweet not found: {tweet_id}")

    tweets = meta.get("tweets") or []
    form = meta.get("form") or ("thread" if len(tweets) > 1 else "single")
    platform_name = "twitter_single" if form == "single" else "twitter_thread"

    if dry_run:
        payload = {
            "tweet_id": tweet_id,
            "dry_run": True,
            "platform": platform_name,
            "would_send": len(tweets),
            "total_chars": sum(t["char_count"] for t in tweets),
        }
        if as_json:
            _emit_json(payload)
            return
        click.echo(f"dry-run: would send {len(tweets)} tweets as {platform_name}")
        return

    version = PlatformVersion(
        platform=platform_name,
        content="\n\n---\n\n".join(t["text"] for t in tweets),
        metadata={"tweets": tweets, "form": form},
        formatting_changes=[],
    )
    publisher = TwitterPublisher(credentials={})
    result = asyncio.run(publisher.publish(version))

    append_publish_record(tweet_id, result)

    # Persist thread ids if present.
    raw = getattr(result, "raw_response", None) or {}
    update_fields = {"status": result.status}
    if result.status in {"success", "partial_success"}:
        update_fields["published_urls"] = [result.published_url]
        if raw.get("thread_tweet_ids"):
            update_fields["thread_tweet_ids"] = raw["thread_tweet_ids"]
        if result.published_at:
            update_fields["published_at"] = (
                result.published_at.isoformat()
                if hasattr(result.published_at, "isoformat")
                else str(result.published_at)
            )
    tw_storage.update_status(tweet_id, **update_fields)

    append_memory_event(
        "tweet_published",
        article_id=tweet_id,
        payload={
            "form": form,
            "status": result.status,
            "url": result.published_url,
            "platform_post_id": result.platform_post_id,
            "thread_tweet_ids": raw.get("thread_tweet_ids"),
            "failure_reason": result.failure_reason,
        },
    )

    out = {
        "tweet_id": tweet_id,
        "platform": result.platform,
        "status": result.status,
        "published_url": result.published_url,
        "platform_post_id": result.platform_post_id,
        "thread_tweet_ids": raw.get("thread_tweet_ids"),
        "failure_reason": result.failure_reason,
    }
    if as_json:
        _emit_json(out)
        return
    click.echo(
        f"{result.platform}: {result.status} "
        f"{result.published_url or result.failure_reason or ''}"
    )


@cli.command("tweet-rollback", help="Delete the published tweet(s) of a thread.")
@click.argument("tweet_id")
@click.option("--json", "as_json", is_flag=True, default=False)
def tweet_rollback(tweet_id: str, as_json: bool) -> None:
    from agentflow.agent_d4.publishers.twitter import TwitterPublisher
    from agentflow.agent_d4.storage import append_rollback_record
    from agentflow.agent_tw import storage as tw_storage
    from agentflow.shared.memory import append_memory_event

    try:
        meta = tw_storage.load(tweet_id)
    except FileNotFoundError:
        raise click.ClickException(f"tweet not found: {tweet_id}")

    thread_ids = meta.get("thread_tweet_ids") or []
    primary = (
        thread_ids[0]
        if thread_ids
        else (meta.get("published_urls") or [None])[0]
    )
    if not thread_ids and not primary:
        raise click.ClickException("no published tweets found for this draft")

    publisher = TwitterPublisher(credentials={})
    ok, reason = publisher.rollback(
        platform_post_id=primary, thread_tweet_ids=thread_ids
    )

    form = meta.get("form") or "single"
    platform_name = "twitter_single" if form == "single" else "twitter_thread"
    append_rollback_record(
        article_id=tweet_id,
        platform=platform_name,
        platform_post_id=primary,
        published_url=(meta.get("published_urls") or [None])[0],
        failure_reason=None if ok else reason,
    )

    if ok:
        tw_storage.update_status(tweet_id, status="rolled_back")

    append_memory_event(
        "tweet_rolled_back",
        article_id=tweet_id,
        payload={
            "platform": platform_name,
            "thread_tweet_ids": thread_ids,
            "ok": ok,
            "failure_reason": reason,
        },
    )

    out = {
        "tweet_id": tweet_id,
        "ok": ok,
        "failure_reason": reason,
        "deleted_count": len(thread_ids) if thread_ids else 1,
    }
    if as_json:
        _emit_json(out)
        return
    click.echo(
        f"rolled back {tweet_id}: {'ok' if ok else 'failed'} — "
        f"{reason or f'deleted {len(thread_ids) if thread_ids else 1} tweet(s)'}"
    )


@cli.command("tweet-list", help="List tweet drafts.")
@click.option(
    "--status",
    type=click.Choice(["all", "draft", "published", "failed", "rolled_back"]),
    default="all",
    show_default=True,
)
@click.option("--since", "since_days", type=int, default=None,
              help="Only tweets created in the last N days.")
@click.option("--json", "as_json", is_flag=True, default=False)
def tweet_list(status: str, since_days: int | None, as_json: bool) -> None:
    from agentflow.agent_tw import storage

    items = storage.list_all()

    if since_days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
        filtered: list = []
        for it in items:
            try:
                ts = datetime.fromisoformat(it.get("created_at", ""))
            except Exception:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts >= cutoff:
                filtered.append(it)
        items = filtered

    if status != "all":
        items = [i for i in items if i.get("status") == status]

    if as_json:
        _emit_json({"count": len(items), "tweets": items})
        return
    if not items:
        click.echo("(no tweets)")
        return
    click.echo(f"{len(items)} tweet(s):")
    for it in items:
        urls = it.get("published_urls") or []
        url = urls[0] if urls else ""
        click.echo(
            f"  {it['tweet_id']:<32}  {it.get('form','?'):<6} "
            f"status={it.get('status','?'):<12} {url}"
        )
