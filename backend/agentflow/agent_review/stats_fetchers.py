"""Best-effort engagement-stat fetchers for published articles.

Each ``fetch_<platform>_stats`` returns a dict with whatever metrics it could
get plus ``fetched_at`` + ``platform_url`` for traceability, or ``None`` when
the platform is unreachable / unconfigured. The router ``fetch_stats`` picks
the right fetcher by name. All scrapes are best-effort with a 20s timeout
and degrade to ``{"scrape_status": "blocked"}`` on any failure — analytics
cron jobs must never crash.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from urllib.parse import quote, urlparse
from typing import Any

_TIMEOUT = 20
_UA = {"User-Agent": "agentflow-stats/0.1"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _shell(url: str, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"fetched_at": _now_iso(), "platform_url": url}
    out.update(extra)
    return out


def _get_html(url: str) -> str | None:
    """GET ``url`` → text on 2xx, None otherwise. Never raises."""
    try:
        import requests
        resp = requests.get(url, timeout=_TIMEOUT, headers=_UA)
        if 200 <= resp.status_code < 300:
            return resp.text
    except Exception:
        pass
    return None


def _get_json(url: str, *, headers: dict[str, str] | None = None, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """GET ``url`` as JSON on 2xx, None otherwise. Never raises."""
    try:
        import requests
        resp = requests.get(
            url,
            timeout=_TIMEOUT,
            headers={**_UA, **(headers or {})},
            params=params,
        )
        if 200 <= resp.status_code < 300:
            data = resp.json()
            return data if isinstance(data, dict) else None
    except Exception:
        pass
    return None


def fetch_medium_stats(url: str) -> dict | None:
    """Scrape claps + responses from a Medium article page.

    TODO: Medium scrape selectors may break if they restructure their SSR
    JSON dumps; keep regexes loose and fall back to scrape_status="blocked".
    """
    html = _get_html(url)
    if html is None:
        return _shell(url, claps=None, responses=None, scrape_status="blocked")
    claps: int | None = None
    responses: int | None = None
    for pat in (r'"clapCount":\s*(\d+)', r'"applauseCount":\s*(\d+)'):
        m = re.search(pat, html)
        if m:
            claps = int(m.group(1)); break
    for pat in (r'"responsesCount":\s*(\d+)',
                r'"postResponses":\s*\{[^}]*"count":\s*(\d+)'):
        m = re.search(pat, html)
        if m:
            responses = int(m.group(1)); break
    if claps is None and responses is None:
        return _shell(url, claps=None, responses=None, scrape_status="blocked")
    return _shell(url, claps=claps, responses=responses, scrape_status="ok")


def fetch_substack_stats(url: str) -> dict | None:
    """Scrape comment/like counts from a Substack post."""
    html = _get_html(url)
    if html is None:
        return _shell(url, likes=None, comments=None, scrape_status="blocked")
    likes: int | None = None
    comments: int | None = None
    m = re.search(r'class="post-ufi-comment-button"[^>]*>\s*(\d+)', html)
    if m:
        comments = int(m.group(1))
    if comments is None:
        m = re.search(r'"comment_count":\s*(\d+)', html)
        if m:
            comments = int(m.group(1))
    m = re.search(r'"reaction":\s*\{[^}]*"count":\s*(\d+)', html) \
        or re.search(r'"likes_count":\s*(\d+)', html)
    if m:
        likes = int(m.group(1))
    if likes is None and comments is None:
        return _shell(url, likes=None, comments=None, scrape_status="blocked")
    return _shell(url, likes=likes, comments=comments, scrape_status="ok")


def _ghost_admin_endpoint(path: str) -> str | None:
    api_url = os.environ.get("GHOST_ADMIN_API_URL")
    admin_key = os.environ.get("GHOST_ADMIN_API_KEY")
    if not api_url or not admin_key:
        return None
    base = api_url.rstrip("/")
    if "/ghost/api/admin" not in base:
        base = f"{base}/ghost/api/admin"
    return f"{base}/{path.lstrip('/')}"


def _ghost_slug_from_url(url: str) -> str | None:
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    return parts[-1] if parts else None


def fetch_ghost_stats(url: str, post_id: str | None = None) -> dict | None:
    """Read Ghost post metadata and count fields through the Admin API."""
    admin_key = os.environ.get("GHOST_ADMIN_API_KEY")
    if not admin_key:
        return None
    try:
        from agentflow.agent_d4.publishers.ghost import _build_jwt

        token = _build_jwt(admin_key)
    except Exception:
        return _shell(url, scrape_status="auth_error")

    slug = _ghost_slug_from_url(url)
    if post_id:
        endpoint = _ghost_admin_endpoint(f"posts/{post_id}/")
    elif slug:
        endpoint = _ghost_admin_endpoint(f"posts/slug/{slug}/")
    else:
        return _shell(url, scrape_status="missing_post_id")
    if not endpoint:
        return None

    data = _get_json(
        endpoint,
        headers={"Authorization": f"Ghost {token}"},
        params={"include": "count.clicks,count.mentions", "formats": "mobiledoc"},
    )
    if data is None:
        return _shell(url, scrape_status="blocked")
    posts = data.get("posts") or []
    if not posts:
        return _shell(url, scrape_status="not_found")
    post = posts[0] or {}
    counts = post.get("count") if isinstance(post.get("count"), dict) else {}
    return _shell(
        url,
        post_id=post.get("id") or post_id,
        title=post.get("title"),
        post_status=post.get("status"),
        visibility=post.get("visibility"),
        published_at=post.get("published_at"),
        updated_at=post.get("updated_at"),
        clicks=counts.get("clicks"),
        mentions=counts.get("mentions"),
        scrape_status="ok",
    )


def fetch_twitter_stats(url: str, post_id: str | None = None) -> dict | None:
    """Fetch public_metrics for a tweet via v2 API. Skip if no bearer token."""
    bearer = os.environ.get("TWITTER_BEARER_TOKEN")
    if not bearer:
        return None
    tid = post_id
    if not tid:
        m = re.search(r"/status(?:es)?/(\d+)", url or "")
        tid = m.group(1) if m else None
    if not tid:
        return _shell(url, scrape_status="missing_post_id")
    try:
        import requests
        resp = requests.get(
            f"https://api.twitter.com/2/tweets/{tid}",
            params={"tweet.fields": "public_metrics"},
            headers={"Authorization": f"Bearer {bearer}", **_UA},
            timeout=_TIMEOUT,
        )
        if resp.status_code != 200:
            return _shell(url, scrape_status=f"http_{resp.status_code}")
        m = ((resp.json() or {}).get("data") or {}).get("public_metrics") or {}
    except Exception:
        return _shell(url, scrape_status="blocked")
    return _shell(
        url, likes=m.get("like_count"), retweets=m.get("retweet_count"),
        replies=m.get("reply_count"), quotes=m.get("quote_count"),
        impressions=m.get("impression_count"), scrape_status="ok",
    )


def _linkedin_post_urn(url: str, post_id: str | None = None) -> str | None:
    if post_id:
        return post_id
    m = re.search(r"urn:li:[A-Za-z]+:\d+", url or "")
    if m:
        return m.group(0)
    m = re.search(r"/feed/update/([^/?#]+)", url or "")
    return m.group(1) if m else None


def fetch_linkedin_stats(url: str, post_id: str | None = None) -> dict | None:
    """Fetch LinkedIn socialActions likes/comments when token scopes allow it."""
    token = os.environ.get("LINKEDIN_ACCESS_TOKEN")
    if not token:
        return None
    urn = _linkedin_post_urn(url, post_id)
    if not urn:
        return _shell(url, scrape_status="missing_post_id")
    endpoint = f"https://api.linkedin.com/v2/socialActions/{quote(urn, safe='')}"
    data = _get_json(
        endpoint,
        headers={
            "Authorization": f"Bearer {token}",
            "X-Restli-Protocol-Version": "2.0.0",
        },
    )
    if data is None:
        return _shell(url, scrape_status="blocked")
    likes = data.get("likesSummary") or {}
    comments = data.get("commentsSummary") or {}
    return _shell(
        url,
        post_id=urn,
        likes=likes.get("totalLikes"),
        comments=comments.get("aggregatedTotalComments"),
        scrape_status="ok",
    )


def fetch_webhook_stats(url: str, post_id: str | None = None) -> dict | None:
    """Call an operator-owned stats endpoint for custom CMS/webhook publishes."""
    endpoint = os.environ.get("AGENTFLOW_WEBHOOK_STATS_URL") or os.environ.get("WEBHOOK_STATS_URL")
    if not endpoint:
        return None
    auth_header = (
        os.environ.get("AGENTFLOW_WEBHOOK_STATS_AUTH_HEADER")
        or os.environ.get("WEBHOOK_AUTH_HEADER")
        or ""
    ).strip()
    headers = {"Authorization": auth_header} if auth_header else None
    data = _get_json(
        endpoint,
        headers=headers,
        params={"platform_url": url, "post_id": post_id or ""},
    )
    if data is None:
        return _shell(url, scrape_status="blocked")
    payload = dict(data)
    status = payload.pop("scrape_status", "ok")
    return _shell(url, scrape_status=status, post_id=post_id, **payload)


_FETCHERS: dict[str, Any] = {
    "medium": fetch_medium_stats, "substack": fetch_substack_stats,
    "ghost": fetch_ghost_stats, "ghost_wordpress": fetch_ghost_stats,
    "twitter": fetch_twitter_stats, "twitter_thread": fetch_twitter_stats,
    "twitter_single": fetch_twitter_stats, "x": fetch_twitter_stats,
    "linkedin": fetch_linkedin_stats, "linkedin_article": fetch_linkedin_stats,
    "webhook": fetch_webhook_stats,
}


def fetch_stats(platform: str, url: str, post_id: str | None = None) -> dict | None:
    """Dispatch to a platform-specific fetcher. Never raises."""
    fn = _FETCHERS.get((platform or "").lower())
    if fn is None:
        return None
    try:
        if fn in {
            fetch_twitter_stats,
            fetch_ghost_stats,
            fetch_linkedin_stats,
            fetch_webhook_stats,
        }:
            return fn(url, post_id)
        return fn(url)
    except Exception:
        return _shell(url, scrape_status="blocked")
