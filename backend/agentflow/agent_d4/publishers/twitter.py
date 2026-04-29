"""Twitter/X publisher — OAuth 1.0a user-context, lazy tweepy import.

Single and thread forms both route through :meth:`publish`; the caller
packs ``PlatformVersion.metadata['tweets']`` with the list of
``{text, image_slot, image_hint, image_path?}`` dicts.

Rollback deletes each tweet in ``thread_tweet_ids`` (or the single
``platform_post_id``) best-effort.
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from hashlib import blake2b
from typing import Any

from agentflow.shared.logger import get_logger
from agentflow.shared.models import PlatformVersion, PublishResult

from .base import BasePublisher

_log = get_logger("agent_d4.twitter")


def _oauth_ready() -> tuple[bool, str | None]:
    required = (
        "TWITTER_CONSUMER_KEY",
        "TWITTER_CONSUMER_SECRET",
        "TWITTER_USER_ACCESS_TOKEN",
        "TWITTER_USER_ACCESS_SECRET",
    )
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        return False, f"missing: {', '.join(missing)}"
    return True, None


class TwitterPublisher(BasePublisher):
    platform_name = "twitter_thread"

    def _mock_url(self, short_hash: str) -> str:
        handle = os.environ.get("TWITTER_HANDLE", "@mock").lstrip("@")
        return f"https://twitter.com/{handle}/status/mock_{short_hash}"

    async def publish(self, version: PlatformVersion) -> PublishResult:
        meta = version.metadata or {}
        tweets: list[dict[str, Any]] = meta.get("tweets") or []
        if not tweets:
            return PublishResult(
                platform=self.platform_name,
                status="failed",
                failure_reason="no tweets in PlatformVersion.metadata['tweets']",
            )

        form = meta.get("form") or ("thread" if len(tweets) > 1 else "single")
        self.platform_name = "twitter_single" if form == "single" else "twitter_thread"

        if self._is_mock_mode():
            return self._mock_publish_thread(tweets)

        ok, reason = _oauth_ready()
        if not ok:
            return PublishResult(
                platform=self.platform_name,
                status="failed",
                failure_reason=reason,
            )

        try:
            import tweepy  # lazy
        except ImportError:
            return PublishResult(
                platform=self.platform_name,
                status="failed",
                failure_reason=(
                    "tweepy not installed — run `pip install tweepy` then retry"
                ),
            )

        client = tweepy.Client(
            consumer_key=os.environ["TWITTER_CONSUMER_KEY"],
            consumer_secret=os.environ["TWITTER_CONSUMER_SECRET"],
            access_token=os.environ["TWITTER_USER_ACCESS_TOKEN"],
            access_token_secret=os.environ["TWITTER_USER_ACCESS_SECRET"],
        )

        v1_auth = tweepy.OAuth1UserHandler(
            os.environ["TWITTER_CONSUMER_KEY"],
            os.environ["TWITTER_CONSUMER_SECRET"],
            os.environ["TWITTER_USER_ACCESS_TOKEN"],
            os.environ["TWITTER_USER_ACCESS_SECRET"],
        )
        api_v1 = tweepy.API(v1_auth)

        posted_ids: list[str] = []
        prev_id: str | None = None
        for t in tweets:
            media_ids: list[str] = []
            img_path = t.get("image_path")
            if img_path and os.path.exists(img_path):
                try:
                    m = api_v1.media_upload(img_path)
                    media_ids.append(str(m.media_id))
                except Exception as exc:
                    _log.warning("media upload failed for %s: %s", img_path, exc)

            try:
                resp = client.create_tweet(
                    text=t["text"],
                    in_reply_to_tweet_id=prev_id,
                    media_ids=media_ids or None,
                )
            except Exception as exc:
                # Partial success: return what we've got.
                return PublishResult(
                    platform=self.platform_name,
                    status="partial_success" if posted_ids else "failed",
                    failure_reason=f"tweet {t.get('index')}: {exc}",
                    platform_post_id=posted_ids[0] if posted_ids else None,
                    published_url=(
                        self._status_url(posted_ids[0]) if posted_ids else None
                    ),
                    raw_response={"posted_ids": posted_ids},
                    published_at=datetime.now(),
                )
            posted_ids.append(str(resp.data["id"]))
            prev_id = resp.data["id"]
            time.sleep(1.5)

        return PublishResult(
            platform=self.platform_name,
            status="success",
            published_url=self._status_url(posted_ids[0]),
            platform_post_id=posted_ids[0],
            raw_response={"thread_tweet_ids": posted_ids},
            published_at=datetime.now(),
        )

    def _mock_publish_thread(self, tweets: list[dict[str, Any]]) -> PublishResult:
        short = blake2b(
            "".join(t.get("text", "") for t in tweets).encode(), digest_size=5
        ).hexdigest()
        posted_ids = [f"mock_{short}_{i}" for i in range(len(tweets))]
        _log.info("MOCK_LLM=true — skipping real tweepy, returning %d fake ids", len(tweets))
        return PublishResult(
            platform=self.platform_name,
            status="success",
            published_url=self._mock_url(f"{short}_0"),
            platform_post_id=posted_ids[0],
            raw_response={"thread_tweet_ids": posted_ids, "mock": True},
            published_at=datetime.now(),
        )

    def _status_url(self, tweet_id: str) -> str:
        handle = (os.environ.get("TWITTER_HANDLE") or "@me").lstrip("@")
        return f"https://twitter.com/{handle}/status/{tweet_id}"

    def rollback(
        self, platform_post_id: str, thread_tweet_ids: list[str] | None = None
    ) -> tuple[bool, str | None]:
        """Delete the tweet(s). Best effort — Twitter restricts deletion of
        tweets older than a few hours / across account transfers.
        """
        if self._is_mock_mode():
            _log.info(
                "MOCK_LLM=true — pretending to delete %s (+%d in thread)",
                platform_post_id,
                len(thread_tweet_ids or []) - 1 if thread_tweet_ids else 0,
            )
            return True, None

        ok, reason = _oauth_ready()
        if not ok:
            return False, reason

        try:
            import tweepy
        except ImportError:
            return False, "tweepy not installed"

        client = tweepy.Client(
            consumer_key=os.environ["TWITTER_CONSUMER_KEY"],
            consumer_secret=os.environ["TWITTER_CONSUMER_SECRET"],
            access_token=os.environ["TWITTER_USER_ACCESS_TOKEN"],
            access_token_secret=os.environ["TWITTER_USER_ACCESS_SECRET"],
        )

        ids_to_delete = thread_tweet_ids or [platform_post_id]
        failures: list[str] = []
        for tid in reversed(ids_to_delete):  # delete children first
            try:
                client.delete_tweet(tid)
            except Exception as exc:
                failures.append(f"{tid}: {exc}")
            time.sleep(0.5)

        if failures:
            return False, f"partial delete: {'; '.join(failures[:3])}"
        return True, None
