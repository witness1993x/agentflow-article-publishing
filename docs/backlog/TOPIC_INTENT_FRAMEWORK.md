# TopicIntent — 跨 flow 的话题意图抽象（策略 MEMO）

> Status: 策划中 | Updated: 2026-04-24 | Owner: D1 / skill 编排 / Memory 消费

## 1. 为什么需要一个"意图"抽象

当前 AgentFlow 的所有 CLI 是**被动消费式**的：
- `af hotspots` = "给我看今天 KOL 聊了什么"
- `af write <hid>` = "把这条 hotspot 写成文"
- `af preview/publish` = "发到我常用的平台"

但真实创作者的工作流经常是**主动检索式**的：
- "我今天想写一篇关于 `multi-agent orchestration` 的，给我材料"
- "最近 `MCP server` 有什么新动静"
- "Vitalik 最近谈 consciousness 的那几条我要跟一篇"
- "每周一固定写 AI × 投资，星期四写 Web3 × governance"

**话题意图（TopicIntent）** 就是这个"我想关注什么"的一等公民表达。它会进入：

- D1：定向扫描、筛选聚类
- D2：写作时让 draft 中心围绕意图，不是围绕 Kimi 随手的包装概念（hallucination 风险减少）
- Step 1b：pre-publish overview 里核对"文章中心 vs 当初意图"
- Memory：沉淀用户的意图模式（"每周一早上都搜 X" → 下次周一默认扫 X）

## 2. 设计目标

- **跨 CLI 一致**：同一个意图结构能喂给 `hotspots / search / write / preview`
- **来源可叠加**：CLI flag / user_profile / preferences / 对话上下文 / 命令行历史 —— 谁都可以贡献一块
- **可被 Memory 消费**：意图本身也是一种行为事件，进入 `events.jsonl`
- **可被用户否决**：永远允许 `--no-intent` 走空
- **语言无关**：中英双语 query，regex + 语义双轨

## 3. TopicIntent 数据结构

```yaml
# ~/.agentflow/intents/current.yaml (运行时；非提交)
schema_version: 1
created_at: 2026-04-24T...
source: cli_flag | profile | preferences | conversation | memory_recall
# 谁贡献了这个意图

query:
  text: "multi-agent orchestration"       # 人类可读的 query
  lang: auto                               # auto | zh | en | mixed
  mode: keyword | semantic | regex         # 默认 keyword
  must_include: ["orchestration"]          # 必须出现
  should_include: ["agent", "multi"]       # 加分项
  must_exclude: ["煽动", "spam"]           # 排除

filters:
  sources: [twitter, rss, hackernews]      # 限制源
  authors: ["@simonw", "@karpathy"]        # 限 KOL
  date_range: {days: 7}                    # 最近 N 天
  min_engagement: {like_count: 20}         # 最低互动

profile_context:                           # 和 user 风格/系列的关系
  target_series: A                         # 期望归到哪个 series
  voice_fit_required: true                 # 强制走 style_profile 里的 voice_principles

metadata:
  purpose: daily_scan | ad_hoc_search | weekly_column | follow_up
  notes: "周一 AI × 投资栏"
  ttl: single_use | session | persistent   # 意图存活期
```

### 关键字段说明

- **`source`**：区分"用户这次显式说要 X"（`cli_flag`）vs "从上次发稿默认继承"（`preferences`）。Skill 在给用户展示时应标明来源，避免"AI 替我决定了话题"的错觉。
- **`mode`**：
  - `keyword` = 简单 substring/regex 匹配（v0.1）
  - `semantic` = Jina 嵌入后向量检索（v0.5，需要 embedding 历史库）
  - `regex` = 完整正则
- **`ttl`**：一次性 / 当前 session / 永久。对应三种用法：
  - "这次帮我找 X" → `single_use`
  - "接下来半小时都围绕 X 来" → `session`
  - "我每周一都写 X 栏目" → `persistent`（进 preferences）

## 4. 意图的 4 个来源

```
┌───────────────┐     ┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│ 1. CLI flag   │     │ 2. profile   │     │ 3. preferences│     │ 4. memory    │
│ --filter X    │     │ style_profile│     │ recent_intents│     │ conversation │
│ --query X     │     │ .focus_topics│     │  (Memory 产物)│     │ /skill 对话  │
└───────┬───────┘     └──────┬───────┘     └──────┬───────┘     └──────┬───────┘
        │                    │                    │                    │
        └────────────────────┴────────────────────┴────────────────────┘
                                     │
                                     ▼
                         ┌─────────────────────┐
                         │  merge_intent(...)  │
                         │  (叠加 + 去重 + 权重) │
                         └──────────┬──────────┘
                                    │
                                    ▼
                           ~/.agentflow/intents/current.yaml
                                    │
                                    ▼
                  ┌─────────────────┴─────────────────┐
                  │     intent-aware CLI commands      │
                  │ (hotspots / search / write / etc.) │
                  └────────────────────────────────────┘
```

**合并规则**（简单先行）：
- 显式来源（CLI flag / conversation）覆盖隐式来源（profile / preferences）
- `must_include` 取并集
- `must_exclude` 取并集
- `filters.sources` 取交集
- `filters.date_range` 取最严
- 冲突时以 CLI flag 为准

## 5. CLI 入口设计

### 5.1 `af hotspots --filter <regex>` ★ v0.1 Slice
- 跑完整 D1 扫描后，对每个 hotspot 的 `topic_one_liner + suggested_angles[*].title + source_references[*].text_snippet` 做 regex 匹配
- 命中任一字段即保留
- 不改原扫描行为（不减少 Twitter/RSS/HN API 调用）
- 写 memory event `topic_intent_used` 含 `{mode: keyword, query: X, matched: N, total: M}`

### 5.2 `af hotspots --query <text>` 语义增强版
- 同 5.1 但用 Jina embed `query` → 每个 cluster 的 topic embed 余弦相似度 ≥ 阈值才保留
- 需要 hotspot 生成时就预存 topic embedding（改 D1 clustering.py 顺便存）
- v0.5

### 5.3 `af search <query>` 独立检索 ★ v0.3 Slice
- 不走订阅式扫描，直接对 query 打外部 API：
  - Twitter Search v2（需付费档 bearer token 或改 tweepy 的 `search_recent_tweets`）
  - HN Algolia API（`https://hn.algolia.com/api/v1/search?query=X`，免费）
  - Google CSE / Kagi search（可选）
- 产出 one-off hotspot 写到 `~/.agentflow/hotspots/search_<slug>.json`
- 不覆盖当天 `<date>.json`
- 用同一套 Jina 聚类 + Kimi 独立观点

### 5.4 `af intent-set <query> [--ttl persistent]`
- 显式设置当前意图
- 后续所有 CLI 默认读取（除非 `--no-intent`）
- `--ttl persistent` 写进 `preferences.yaml` 持久化

### 5.5 `af intent-show` / `af intent-clear`
- 检查当前意图是什么、来自哪个源
- 清除当前意图

## 6. 跨命令的 intent 流转

| 命令 | 如何用 intent |
|---|---|
| `af hotspots` | 扫 + post-filter（5.1）或限制 `sources.yaml` 订阅里带匹配 KOL 的子集 |
| `af search <q>` | query 本身就是 intent |
| `af write <hid>` | 传 intent 给 D2 skeleton prompt，让 "article 中心论点必须对齐 intent.query"。**这会直接降低 hallucination**（昨天的"量子纠缠"案例就是 intent 与 refs 不对齐） |
| `af preview` / `af publish` | Step 1b 里展示 "intent was X, article claims Y" 的对齐度 |
| `af memory-tail` | 可显示最近 N 个 `topic_intent_used` 事件 |

## 7. 与 User Profile 的关系

`style_profile.yaml`（D0 产物）**应**新增一个可选字段：

```yaml
focus_topics:               # 用户长期关注的话题轴
  primary:
    - "AI agents + tools"
    - "multi-agent orchestration"
    - "Web3 governance"
  secondary:
    - "developer experience"
    - "consciousness & AI"
  avoid:
    - "politics"
    - "celebrity gossip"
```

`focus_topics` 作为 **profile 级 intent 默认值**：
- `af hotspots` 无 `--filter` 时可选择性用 `focus_topics.primary` post-filter
- `af write` 时提醒 "这篇文章的 intent 是否符合 focus_topics？"
- D0 `learn-style` 可从样本文章自动推断（v0.7+）

**与 `preferences.yaml` 区别**：
- `style_profile.focus_topics` = **稳定的长期兴趣**（用户声明）
- `preferences.recent_intents` = **近期使用模式**（Memory 消费层推断）

## 8. 与 Memory 消费层的闭环

每次 CLI 带 intent 跑完后，写 `topic_intent_used` 事件：

```json
{
  "event_type": "topic_intent_used",
  "article_id": null,         // intent 可以不跟特定文章
  "payload": {
    "query_text": "MCP server",
    "mode": "keyword",
    "source": "cli_flag",
    "command": "hotspots",
    "matched_count": 3,
    "total_count": 8,
    "ttl": "single_use"
  }
}
```

`af prefs-rebuild`（Memory 消费层，见 `MEMORY_TO_DEFAULTS.md`）消费这些事件：

```yaml
# preferences.yaml 新字段
intent:
  recent_queries:           # 频率倒序
    - query: "multi-agent orchestration"
      uses: 12
      last_used: 2026-04-23
    - query: "MCP server"
      uses: 8
      last_used: 2026-04-20
  time_patterns:            # 时间模式
    monday_morning: ["AI × investment"]
    thursday_evening: ["Web3 × governance"]
  _source_events: 34
```

未来 CLI 启动时可以提示："现在是周一早上，你上 3 个周一都扫了 `AI × investment` — 要用这个 intent 吗？"

## 9. v0.1 - v0.7 Roadmap

### v0.1（本轮实现）
- `af hotspots --filter <regex>` — post-filter
- `topic_intent_used` memory event
- skill 层识别用户一开口就带话题的意图
- MEMO 文档化

### v0.3
- `af search <query>` — HN Algolia 免费查 + Twitter Search（如果用户有付费 bearer）
- `af intent-set / show / clear`
- D2 prompt 接收 intent，避免 hallucination
- Step 1b 展示 "intent vs article 对齐度"

### v0.5
- Jina embedding 语义 query（`--query --semantic`）
- `style_profile.focus_topics` 落地 + D0 auto-infer
- `af prefs-rebuild` 消费 `topic_intent_used`

### v0.7+
- 时间模式识别（周一早上 → 特定 intent）
- intent 的强度衰减 / 显式 forget
- 跨文章 intent 连续性（"follow up the last 3 articles on X"）

## 10. 决策点

| # | 决策 | 建议 |
|---|---|---|
| T1 | v0.1 用 regex 还是简单 substring？ | **regex**（更灵活；用户可以 `--filter "MCP\|agent"`） |
| T2 | `--filter` 匹配字段？ | `topic_one_liner + suggested_angles[*].title + source_references[*].text_snippet` 取并集 |
| T3 | 空 filter 时写 memory event 吗？ | 不写（避免噪声） |
| T4 | 匹配结果为 0 怎么办？ | 返回空数组 + stderr 提示 "0 matched, consider broadening query"；不 fallback 到原始结果 |
| T5 | `focus_topics` 进 `style_profile` 还是独立 `focus.yaml`？ | 独立 `focus.yaml` — 职责更清晰，style 是文本风格，focus 是话题兴趣 |
| T6 | `af search` v0.3 是否先只接 HN Algolia？ | 是，Twitter Search 要钱，延后 |

## 11. 不做什么

- 不做 intent 的 NLU 解析（"帮我找最近的 AI 新闻" → 不试图拆成结构化 intent）
- 不做意图推荐系统
- 不做 intent 冲突自动解决（冲突时降级为 CLI flag 优先，手动拍板）
- 不在 `style_profile.focus_topics` 里做 NLP 推断（user 自己填或从文章样本抽取）
- 不跨用户共享 intent（本地优先原则）

## 12. 对当前系统的改动面

| 文件 | 改动 | 切片 |
|---|---|---|
| `backend/agentflow/agent_d1/main.py` | `run_d1_scan` 接收 `filter: str \| None` | v0.1 |
| `backend/agentflow/cli/commands.py` | `hotspots` 子命令加 `--filter`；在 event 里写 `topic_intent_used` | v0.1 |
| `.claude/skills/agentflow-hotspots/SKILL.md` | 加"用户一开口带话题 → `--filter`"编排 | v0.1 |
| `.claude/skills/agentflow/SKILL.md` | 顶层提一句 "所有 skill 都可以带话题意图" | v0.1 |
| `backend/agentflow/shared/memory.py` | 新事件类型 `topic_intent_used`（schema 无需改，只是新 event_type） | v0.1 |
| `backend/agentflow/agent_d1/clustering.py` | 保存 cluster 的 Jina embedding 到 hotspot json | v0.5 |
| `backend/agentflow/cli/commands.py` | 新增 `search / intent-set / intent-show / intent-clear` 命令 | v0.3 |
| `backend/agentflow/shared/models.py` | 新增 `TopicIntent` dataclass | v0.3 |

v0.1 改动面很小（2 文件 + 2 skill 小补），可以本轮做。
