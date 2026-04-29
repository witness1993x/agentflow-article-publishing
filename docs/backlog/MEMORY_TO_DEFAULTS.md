# Memory → Default Strategy（Phase 1 设计 MEMO）

> Status: 策划中 | Updated: 2026-04-24 | Owner: D2/D3 链路

## 1. 问题

当前 `~/.agentflow/memory/events.jsonl` 记录了 9 种事件（`article_created / fill_choices / section_edit / hotspot_review / preview / publish / publish_rolled_back / learn_style / image_resolved`），但系统**只记录、不消费**。

结果：每写一篇文章，用户都要重新选 title/opening/closing index；每次 preview 都要从头选平台；每次 publish 默认假设都跟第一次一样。用户的"模式"完全没被利用。

## 2. 目标

让 AgentFlow 在第 N+1 次执行任务时，默认值来自前 N 次的历史行为，**且用户能解释这些默认值从哪来**。

反目标：

- **不做黑盒自动决策**。每个默认值要能追溯到具体事件。
- **不污染 `metadata.json`**。跨篇偏好单独落盘。
- **不引入数据库或索引**。消费层只是 `events.jsonl` 的纯函数聚合。
- **不自动修改 `style_profile.yaml`**。风格是 D0 的责任边界。

## 3. 可消费的事件 / 可生成的默认值

| 事件 | 可提取的默认 | 生效点 | 置信度门槛 |
|---|---|---|---|
| `fill_choices` | title_index / opening_index / closing_index 的偏好分布 | `af write --auto-pick` 选择时（替代硬编码 0/0/0） | ≥ 3 次才启用 |
| `section_edit` | 用户倾向用的编辑命令（改短/加例子/去AI味）、常编辑的 section 位置 | Step 3 edit loop 的"建议命令" | ≥ 5 次 |
| `publish`（success） | 常用平台集合、prefer `GHOST_STATUS=published` vs `draft` | `af preview / publish` 默认 `--platforms`、`GHOST_STATUS` | ≥ 3 次 |
| `publish_rolled_back` | 负信号：最近回滚过 → 下次发稿前降级为 `GHOST_STATUS=draft` 提醒 | publish 前 Step 1b | 单次命中 |
| `hotspot_review` | 跳过/保存的 hotspot 类型（series、author、topic 关键词） | D1 scan 后 rerank | ≥ 10 次 |
| `image_resolved` | 常用本地图库路径前缀 | `af image-resolve` 自动补全候选 | ≥ 5 次 |
| `learn_style` | — | — | 不消费（D0 自己管） |
| `article_created` | 偏好的 target_series 分布 | `af write` 默认系列 | ≥ 3 次 |
| `preview` | 常用平台集合（冗余于 publish） | 同 publish | 同上 |

## 4. 设计

### 4.1 数据流

```
events.jsonl           preferences.yaml            CLI / skill
   │                        ▲                          │
   │ append-only            │ nightly or on-demand     │ reads on start
   │                        │ aggregate (pure fn)      ▼
   └──────────────► af prefs-rebuild                defaults applied
                           │
                           ▼
                   preferences.yaml
```

### 4.2 preferences.yaml schema (v0.1)

```yaml
schema_version: 1
last_computed: 2026-04-24T...Z
source_events: 127  # 消费了多少条事件
notes: |
  每个字段下都带 source_events 和 evidence，用户可追查来源。

write:
  default_title_index: 1          # 1-of-N
  default_opening_index: 0
  default_closing_index: 2
  target_series_weights:          # series 的历史选择频率
    A: 0.72
    B: 0.28
  _confidence: 0.85
  _source_events: 14              # 基于最近 14 次 fill_choices
  _evidence:
    - event_ts: 2026-04-22T...Z
      article_id: hs_2026...-abc
      payload: {title: 1, opening: 0, closing: 2}

preview:
  default_platforms: [ghost_wordpress, linkedin_article]
  _confidence: 0.95
  _source_events: 18
  _evidence: [...]

publish:
  ghost_status: published          # or "draft" if recent rollback
  force_strip_images: false
  _confidence: 0.90
  _source_events: 9
  _evidence: [...]
  _negative_signals:
    - event_ts: 2026-04-24T...Z
      event_type: publish_rolled_back
      note: "recent rollback → downgrade ghost_status to draft for next 3 runs"

edit:
  preferred_commands:              # 频率倒序
    - 去AI味: 0.35
    - 改短: 0.28
    - 加例子: 0.20
    - 改锋利: 0.10
    - 展开: 0.07
  _source_events: 23

image:
  resolved_path_prefixes:          # 最近 N 次 image_resolved 的路径前缀
    - /Users/witness/Pictures/agentflow/: 0.6
    - /Users/witness/Desktop/screenshots/: 0.3
  _source_events: 11

hotspot:
  series_preference:
    A: 0.58
    B: 0.32
    C: 0.10
  skipped_authors: [@someone]       # 历史反复 skip 的 KOL
  _source_events: 42
```

关键设计：每个默认值都带 `_confidence / _source_events / _evidence`。**skill 必须向用户展示这些 metadata**，不是静默用上。

### 4.3 消费入口

**`af prefs-rebuild`**（新）
- 读 `events.jsonl` 全量 → 聚合 → 写 `preferences.yaml`
- 纯函数，可随时重跑
- `--dry-run` 打印将要写的内容但不落盘

**`af prefs-show [--key X]`**（新）
- 读 `preferences.yaml` → 整理打印
- `--key write.default_title_index` 只看一项
- 附带 evidence 链接

**`af prefs-explain <key>`**（新）
- 对某项默认值，打印 evidence 的 10 条具体事件
- 帮助用户判断：这个默认值是不是 outdated、要不要手动覆盖

**`af prefs-reset [--key X]`**（新）
- 清除某个字段（或全部），下次 rebuild 重来
- 用户可强制"遗忘"某个模式

### 4.4 默认值的使用

`af write --auto-pick` 里的逻辑变成：

```
read preferences.yaml
if preferences.write._source_events >= 3:
    title_idx = preferences.write.default_title_index
    ...
else:
    title_idx = 0  # 老的硬编码
```

**skill 层必须在用默认值时告诉用户**：

> "使用历史偏好：title index = 1（基于最近 14 次 fill_choices，置信度 0.85）。要用默认 0 吗？"

这是"可解释"落地的关键——用户永远能问"为什么是 1？"然后看到 evidence。

### 4.5 什么时候重算 preferences？

三个选项，我推荐 C：

| 策略 | 优点 | 缺点 |
|---|---|---|
| A. 每次 CLI 启动时 rebuild | 永远最新 | 启动延迟（每次都全量扫 events.jsonl） |
| B. 用户手动 `af prefs-rebuild` | 最可控 | 容易忘 / 长期漂移 |
| **C. 后台 on-write trigger** | 增量廉价、不卡 CLI | 需要写 `events.jsonl` 的地方都调一次 |

C 的实现：`shared/memory.py::append_memory_event` 后追加一个轻量 `maybe_rebuild_prefs()` —— 只在事件数是 10 的倍数或距上次 rebuild >24h 时真的 rebuild。

## 5. 最小切片（v0.5）

**不要一次做全部**。先交付最小可感知切片：

### 5.1 Slice 1 — title/opening/closing 默认偏好
- 消费 `fill_choices` → `preferences.write.default_*_index`
- `af write --auto-pick` 用
- skill 展示 "基于 N 次历史，默认 X"
- **门槛**：N ≥ 3

### 5.2 Slice 2 — 平台集合
- 消费 `publish(status=success)` → `preferences.preview.default_platforms`
- `af preview` 默认使用

### 5.3 Slice 3 — publish 负信号
- 消费 `publish_rolled_back` → 最近 3 次发布降级为 draft
- 在 Step 1b 展示

剩下的（edit 命令偏好、image 路径、hotspot rerank）放 v0.7+。

## 6. 风险与边界

### 6.1 过拟合老偏好
用户前 10 篇都选 title index 2，但第 11 篇真的想要 title 0。如果 skill 自动用 2，用户会被默认"绊倒"。

缓解：**永远展示默认 + 永远允许覆盖**。不做"静默应用"。

### 6.2 空启动问题
新用户没有历史。Slice 1-3 都有 N≥3 门槛，空启动时回退到老的硬编码 `0/0/0`。没问题。

### 6.3 Memory 被"污染"
用户误操作产生的事件会进入 memory。例如连续回滚 5 次同一篇 → 负信号累积。

缓解：`af prefs-reset` 可清字段，重启聚合。

### 6.4 风格漂移 vs 偏好漂移
用户的"偏好"可能随时间变化。聚合时**近期事件权重应更高**（指数衰减，λ=0.05/day 之类），而不是简单平均。

### 6.5 不要反哺 style_profile
`style_profile.yaml` 是 D0 的产物，反映用户的**文本风格**（taboos、voice_principles、paragraph_preferences）。
`preferences.yaml` 是 Memory 消费层的产物，反映用户的**工作流偏好**（哪个 title、哪个平台、哪个编辑命令）。
两者职责不同，永远不要让 prefs 回写到 style。

## 7. 决策点（需要用户 confirm）

| # | 决策 | 建议 |
|---|---|---|
| D1 | 新文件位置？ | `~/.agentflow/preferences.yaml`（和 style_profile.yaml 并列） |
| D2 | preferences 是否应该 git-ignore？ | 应该（`.gitignore` 已经 `~/.agentflow/` 整体 ignore） |
| D3 | rebuild 触发策略？ | C（后台 on-write + 阈值） |
| D4 | skill 层默认展示还是隐式使用？ | **默认展示**。用户可以说 "just use defaults" 压制 |
| D5 | 是否允许用户直接手动编辑 preferences.yaml？ | 允许但警告"下次 rebuild 会覆盖手动编辑" |

## 8. 实现成本估计

| 组件 | 估计 |
|---|---|
| `af prefs-rebuild/show/explain/reset` 命令 | 半天 |
| 聚合器（纯函数，读 events.jsonl → dict） | 半天 |
| `append_memory_event` 加 trigger | 半小时 |
| Slice 1 (fill_choices → write defaults) 消费入口 | 半天 |
| skill 层展示 + 允许覆盖 | 半天 |
| 文档 + 测试 | 半天 |

**总计**：2-3 天一个 v0.5 可感知切片。

## 9. 不做什么（明确边界）

- 不做 ML 模型预测用户偏好
- 不做推荐系统
- 不自动改 D0 的 style_profile
- 不自动改 D1 的 sources.yaml
- 不做 A/B 测试 / multi-armed bandit
- 不把 preferences 同步到云端
