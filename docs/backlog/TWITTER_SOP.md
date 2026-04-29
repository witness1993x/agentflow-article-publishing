# Twitter 内容生产 SOP

> Status: 策划中 | Updated: 2026-04-24
> Scope: Twitter/X 内容的"生成 → 审核 → 发布 → 留档"完整标准作业流程

## 0. Twitter 是一个独立的写作维度，不是"文章的复述"

长博客和 Twitter 不是同一种产品。生硬地把 blog 转成 thread 会变成 AI 味浓的 LinkedIn-style 推。SOP 的第一条底线：**承认 Twitter 是单独的 writing mode**，在工具层给它独立的生产路径。

### 三种 Twitter 产出形态

| 形态 | 字数 | 场景 | v0.1 支持 |
|---|---|---|---|
| **Single tweet** | ≤ 280 | 即兴观点、链接分享、问句 | ✓ MVP |
| **Thread** | 3-15 条串联 | 深度拆解、案例分析、论证链 | ✓ MVP |
| **Quote tweet + 评论** | 转某条 + 自己观点 | 对 KOL 发言做响应 | v0.3 |
| **Long-form (X Premium)** | 无上限 | X 自营长文（2000-25000 字） | v0.5 |

MVP 覆盖 single + thread 就够。

---

## 1. 整体 SOP（四段）

```
┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐
│ Generate │ →  │  Review  │ →  │ Publish  │ →  │ Archive  │
└──────────┘    └──────────┘    └──────────┘    └──────────┘
     │               │               │                │
     af tweet-draft  skill Step 1b   af tweet-publish  publish_history
     af tweet-split  (+ refs check)  (API v2)         + memory event
```

### 每段对应的 CLI + skill

| 段 | CLI 命令 | Skill 承载 |
|---|---|---|
| Generate | `af tweet-draft <hotspot_id>` / `af tweet-draft --from-article <aid>` | `/agentflow-tweet`（新 skill） |
| Review | `af tweet-show <tweet_id>` | 同上 Step 1b 精简版 |
| Publish | `af tweet-publish <tweet_id>` | 同上 |
| Archive | 自动，写 `~/.agentflow/tweets/<tweet_id>/` + `publish_history.jsonl` | 同上 |

---

## 2. Generate 段

### 2.1 入口：两种来源

**来源 A：从 hotspot 直接出 tweet**
```
af tweet-draft <hotspot_id> --form single|thread [--angle N] --json
```
- 读 hotspot 的 topic + refs + suggested_angles
- LLM 产 tweet（single）或 thread（按逻辑拆 3-15 条）
- 落 `~/.agentflow/tweets/<tweet_id>/`

**来源 B：从已有博客文章派生**
```
af tweet-draft --from-article <article_id> --form thread --json
```
- 读 draft.md + metadata
- LLM 抽出文章的 2-3 个核心论点 → 每个论点一条 tweet
- 不是逐段复述，是**提炼**
- 落同路径

### 2.2 Prompt 设计（`backend/prompts/twitter_draft.md`）

核心约束（防 AI 味）：

```text
你在给 {user_handle} 写 Twitter，不是给 LinkedIn 写开场白。

【Twitter Voice 铁律】
1. 不用 "今天我想分享 / 让我们一起"（LinkedIn-style 开场）
2. 不用 🚀📈💡🔥 这类 emoji，除非原文已经有
3. 不写 "Thread 👇" / "1/" 这类导航前缀
4. 一条推就一个论点，不要塞三个
5. 如果 thread，每条之间必须能"独立被转发" — 不能是残句

【长度】
- Single: 220-275 字符（留 5-60 buffer for quote/handle tagging）
- Thread: 每条 220-275，thread 总长 3-15 条

【风格贴合 style_profile.yaml】
- 读 voice_principles，遵守 taboos.vocabulary
- 优先用作者历史文章里真实出现过的术语

【输出 JSON】
{
  "form": "single | thread",
  "tweets": [
    {"index": 0, "text": "...", "char_count": 247, "image_slot": null | "cover | inline"},
    ...
  ],
  "intended_hook": "发这条的吸引点是什么 — 用于 review 环节",
  "source_refs": [ref_index, ...]   // 用了哪几条 hotspot.source_references
}
```

### 2.3 自动拆 thread

Single → Thread 降级规则：如果 LLM 在 single 模式下产出 > 275 字符，CLI 自动提示 "too long for single, switch to thread? (yes/retry)" 而不是硬截断。

Thread 每条的上下文：LLM 一次性生成整个 thread，**而不是逐条生成**（避免丢前后文）。

### 2.4 落盘结构

```
~/.agentflow/tweets/<tweet_id>/
├── metadata.json         {tweet_id, form, hotspot_id | article_id, created_at, status}
├── tweets.json           完整 thread 数组
├── tweets.md             人类可读版（每条前加 ---）
└── images/               可选图片（Twitter 每推 ≤ 4 图）
```

`<tweet_id>` 格式：`tw_<YYYYMMDDHHMMSS>_<hash>`

### 2.5 写 memory event

`tweet_draft_created`：`{tweet_id, form, tweet_count, source_type: hotspot|article, source_id}`

---

## 3. Review 段

### 3.1 `af tweet-show <tweet_id>` 输出

```
Tweet tw_20260424... (thread, 5 tweets)
  Derived from: hs_20260423_003 (意识是否独立于物理实体存在)
  Intended hook: 用量子纠缠串起 Vitalik substrate-independence 的论点

Tweet 1/5  (247 ch)
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Vitalik 说意识是 substrate-independent 的，理由是大脑也只是物理系统。
  但他忽略了一个东西：如果你能完美模拟物理，为什么模拟出来的意识一定
  是"你"，而不是另一个意识？
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Tweet 2/5  (265 ch)
  [image: quantum-entanglement-diagram.png]
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  ...

Refs used: [1, 3]
  [1] @VitalikButerin https://twitter.com/... "consciousness is substrate-independent..."
  [3] @VitalikButerin https://twitter.com/... "Brains, humans and the Earth are part of the universe..."
```

### 3.2 Step 1b 精简版（发推前）

比 blog 的 Step 1b 短，但保留关键：

- **Lineage**：来自哪个 hotspot/article
- **References**：每条推用了哪几条 refs；flag "hook/central_claim 不在 refs 里"
- **Char count**：每条是否 ≤ 280
- **Handle/mention**：thread 里 @ 了谁（避免意外 @）
- **图片**：每推 ≤ 4 张；总图片数
- **Hashtag 检查**：是否插了 hashtag（作者风格里不用 → flag）

### 3.3 编辑循环

```
af tweet-edit <tweet_id> --index N --command "改短 / 去AI味 / 加例子"
af tweet-edit <tweet_id> --split       # 把 tweet N 拆成两条（thread 变长）
af tweet-edit <tweet_id> --merge N,N+1 # 把相邻两条合并（thread 变短）
af tweet-edit <tweet_id> --reorder "0,3,1,2,4"  # 改顺序
```

每次编辑写 `tweet_edited` event。

### 3.4 预发（dry-run）

```
af tweet-dry-run <tweet_id>
```
- 模拟发推 payload 构造，不真发
- 打印 JSON payload + 预期 API endpoint
- 用于排查 API schema 问题

---

## 4. Publish 段

### 4.1 Twitter API 现状

当前 `.env` 里有 `TWITTER_BEARER_TOKEN`，但 **bearer token 只能读不能写**（只能 GET）。要发推需要：

- OAuth 1.0a (user context) **或** OAuth 2.0 user token
- 需要 user token + secret（从 developer.twitter.com app 的 Keys 页面拿 User access token / secret）
- 或走 OAuth 2.0 PKCE flow（更现代，但需要浏览器一次性授权）

**建议**：v0.1 走 **OAuth 1.0a with user tokens**（一次性从 dev portal 拿到贴进 `.env`，不跑 OAuth flow）：

```
# .env 新增
TWITTER_USER_ACCESS_TOKEN=...
TWITTER_USER_ACCESS_SECRET=...
TWITTER_CONSUMER_KEY=...
TWITTER_CONSUMER_SECRET=...
```

用 `tweepy.Client` 构造：
```python
client = tweepy.Client(
    consumer_key=..., consumer_secret=...,
    access_token=..., access_token_secret=...,
)
```

### 4.2 `af tweet-publish <tweet_id>`

```
af tweet-publish <tweet_id> [--dry-run] [--json]
```

实际逻辑：

```python
# Single
response = client.create_tweet(text=tweets[0].text, media_ids=...)
tweet_url = f"https://twitter.com/{user_handle}/status/{response.data['id']}"

# Thread
prev_id = None
for t in tweets:
    if media := t.image_slot:
        media_id = client.media_upload(local_path).media_id
    else:
        media_id = None
    resp = client.create_tweet(
        text=t.text,
        in_reply_to_tweet_id=prev_id,
        media_ids=[media_id] if media_id else None,
    )
    prev_id = resp.data["id"]
    sleep(1.5)  # rate limit buffer
```

### 4.3 失败处理

- Rate limit (429) → 等 `x-rate-limit-reset`，最多重试 3 次
- 半发成功（thread 发到第 3 条失败）→ 记录已发条 ID，**不自动回滚**（Twitter API 的删推需要独立调用，且时间窗有限）。由用户决定：
  - `af tweet-resume <tweet_id>` 从断点继续
  - `af tweet-rollback <tweet_id>` 删除已发的所有条（best-effort）

### 4.4 Image 上传

Twitter API v2 发推需要先上传 media 拿 `media_id`：

```python
# v1.1 上传接口（v2 还没完全替代这块）
api = tweepy.API(oauth1_auth)
media = api.media_upload(local_path)
media_id = media.media_id
```

要在 `~/.agentflow/tweets/<tid>/images/` 里维护本地图，复用 `af image-resolve` 的 placeholder 机制。

---

## 5. Archive 段

### 5.1 publish_history.jsonl 扩展

现有 schema 支持 Twitter，只需 `platform` 字段用 `twitter_single` / `twitter_thread`：

```json
{
  "article_id": "tw_20260424...",
  "platform": "twitter_thread",
  "status": "success | partial_success | failed | rolled_back",
  "published_url": "https://twitter.com/.../status/first_id",
  "platform_post_id": "first_tweet_id",
  "thread_tweet_ids": ["id1", "id2", ...],   // 新字段，thread-only
  "published_at": "...",
  "failure_reason": null
}
```

`thread_tweet_ids` 允许 rollback 定位每一条。

### 5.2 metadata 更新

`~/.agentflow/tweets/<tid>/metadata.json` 写入：

```json
{
  "tweet_id": "tw_...",
  "status": "published",
  "published_urls": ["https://twitter.com/.../status/id1", ...],
  "published_at": "..."
}
```

### 5.3 Memory event

`tweet_published`：`{tweet_id, form, tweet_count, thread_tweet_ids, published_urls, duration_ms}`

### 5.4 检索

```
af tweet-list [--status all|draft|published|failed] [--since DAYS]
```

列出所有 tweets 状态。类似 `af draft-show` 但针对 tweets。

---

## 6. 新 skill：`/agentflow-tweet`

创建 `.claude/skills/agentflow-tweet/SKILL.md`：

```
---
name: agentflow-tweet
description: Generate, review, publish a Twitter single or thread from a hotspot or article.
---

Wraps af tweet-draft / tweet-show / tweet-edit / tweet-publish / tweet-rollback.

## Input
Expect either `<hotspot_id>` or `<article_id>` from user invocation.

## Step 0 — detect form
"发一条推 / single tweet" → single
"串一个 thread / 长推 / 多条" → thread (default 5-7)
"围绕 X 写一条" → single

## Step 1 — generate
    af tweet-draft <id> --form <form> [--angle N] --json

## Step 2 — review (Step 1b Twitter 精简版)
...

## Step 3 — edit loop
...

## Step 4 — publish
    af tweet-publish <tid> --json
    
## Step 5 — archive + report
```

---

## 7. 实现顺序（2 周）

### Week 1 — Generate + Review
- Day 1: `af tweet-draft` (prompt, 落盘)
- Day 2: `af tweet-show` + Review 格式
- Day 3: `af tweet-edit` 全部子动作（改短/split/merge/reorder）
- Day 4: skill agentflow-tweet 落地
- Day 5: mock 全流程测试

### Week 2 — Publish + Archive
- Day 1-2: Twitter OAuth 1.0a 接入 + `af tweet-publish` single
- Day 3: Thread 分推 + rate limit 处理
- Day 4: Image upload + rollback
- Day 5: 真实 key 发一条测试推 + 归档校验

---

## 8. 决策点

| # | 决策 | 建议 |
|---|---|---|
| T1 | v0.1 是否支持 quote tweet？ | 否。简单先做 original + thread |
| T2 | OAuth 1.0a vs 2.0 PKCE？ | 1.0a（静态 token，一次配好不用 OAuth flow） |
| T3 | Thread 失败半路是否自动回滚？ | 否，提示用户手动决定（rollback 可能删不了超过 24h 的推） |
| T4 | 是否支持 scheduled tweet？ | v0.1 否，v0.3 考虑（需要本地后台或 Twitter 自家调度 API） |
| T5 | Rate limit 重试次数？ | 3 次，间隔按 `x-rate-limit-reset` |
| T6 | 是否做 tweet analytics 追踪（like/retweet 回写）？ | v0.3 可选；memory event 加 `tweet_engagement_snapshot` |

---

## 9. 风险

1. **Twitter API 政策变动频繁**（2023 以来多次 breaking change）→ 封装一个 `publishers/twitter.py` 层，policy 变了只改它
2. **Rate limit 比想象严格**（免费 tier 每 15 分钟 50 条）→ thread 发送加 sleep，skill 层告知用户 quota
3. **Twitter 审核机制**（新账号发推容易被 shadowban）→ 不是代码问题，但要在 skill 提醒首推前让用户确认
4. **Image upload 的 v1.1 API 可能被 deprecated** → 写一个抽象层，未来切 v2 只改这层
5. **用户把长文章直接 "转"成 thread 会出 AI 味** → prompt 强约束 "只提炼不复述"

---

## 10. 不做什么

- 不做 Twitter 数据分析 dashboard
- 不做定时发推调度（v0.1）
- 不做 DM 收发
- 不做 Twitter Spaces
- 不做自动关注/点赞/转推
- 不做反检测 / 绕过 shadowban
