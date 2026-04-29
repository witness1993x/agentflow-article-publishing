# 架构评估：Skill-first vs App？

> Status: 评估 | Updated: 2026-04-24
> 触发：业务复杂度从 "博客生产" 扩展到 "博客 + Twitter + Newsletter + 图片 + 记忆消费"，需要回头问一次 UX 形态是否还合适

## 1. 复杂度体检：现在到底长什么样？

### 1.1 当前 CLI 命令数

截至 2026-04-24（本 session 结束时）：

- 原有 13 条
- 本 session 新加 3 条（`publish-rollback`, `search`, `intent-set/show/clear` 合 3 条）
- **合计 16 条**

按 IMAGE / TWITTER / EMAIL 的规划再加：
- image: `propose-images`, `image-auto-resolve`（另外图生成 v0.7）→ +2
- twitter: `tweet-draft`, `tweet-show`, `tweet-edit`, `tweet-publish`, `tweet-rollback`, `tweet-list`, `tweet-resume`, `tweet-dry-run` → +8
- email: `newsletter-draft`, `newsletter-show`, `newsletter-edit`, `newsletter-preview-send`, `newsletter-send`, `newsletter-list-*`, `notify` → +7-8

**终态大约 33-35 条 CLI 命令** — 远超人类短时记忆。

### 1.2 当前 skill 数

- 现有：`agentflow / -style / -hotspots / -write / -publish` = 5
- 新加：`-tweet / -newsletter / -image`（或 image 并入 write）= 7-8

### 1.3 状态复杂度

`~/.agentflow/` 将会有：

```
style_profile.yaml
preferences.yaml                  (Memory 消费层产物)
focus.yaml                        (话题意图持久化)
intents/current.yaml
sources.yaml
hotspots/<date>.json + search_*.json
drafts/<aid>/...                   (blog drafts)
tweets/<tid>/...                   (tweet drafts)
newsletters/<nid>/...              (email drafts)
publish_history.jsonl              (4 类通道混杂)
memory/events.jsonl                (15+ 种 event_type)
logs/...
images/ (local lib)
```

**同一篇"idea"可能横跨**：1 个 hotspot → 1 个 blog draft → 1 个 twitter thread → 1 封 newsletter → 4 组 publish history 记录。用户需要追踪"这篇东西现在到哪一步了？4 个通道分别发了吗？哪些需要 rollback？"

**这就是典型的 app 需要可视化的场景**。

---

## 2. Skill-first 适合和不适合的地方

### 2.1 适合（继续做核心）

| 能力 | 为什么 skill 合适 |
|---|---|
| 自然语言驱动的生成 | LLM 交互是 skill 天生的形态 |
| 逐篇的编辑循环 | 对话式 loop 比拉表单快 |
| 快速原型 | 改 prompt 即改行为，不用部署 |
| 单用户本地 | 无多端同步需求 |
| 作者一个人一次处理一篇 | 线性串行工作流 |

### 2.2 不适合（需要 app 帮忙）

| 场景 | 为什么 skill 不够 |
|---|---|
| **追踪 20 篇 in-flight 文章的状态** | skill 每次只聚焦一篇，跨篇全景靠用户记忆 |
| **调度（定时发推 / 周一早 8 点发 newsletter）** | Claude Code 没有常驻进程，定时只能靠外部 cron |
| **订阅者列表管理**（几百几千人） | 对话式无法翻页、筛选、排序 |
| **看发布时间线 / 节奏** | 时间轴是典型 UI 形态 |
| **webhook 接收**（email bounce / tweet engagement） | 需要常驻 HTTP 端点 |
| **多文章归档检索** | "上次我写过 MCP 的那篇在哪？" — grep 文件能做但不优雅 |
| **发布前的多通道对齐视图** | 看同一篇 idea 在 blog/twitter/email 的全貌 |
| **协作**（未来扩展） | 不在 v0.x 但需要不要堵死 |

---

## 3. 三个选项

### 选项 A — 继续纯 skill / CLI（保持现状）

- ✅ 优点：零后端、极简、prompt 迭代快
- ❌ 缺点：随复杂度线性变痛苦；scheduled 任务做不了；没有全景视图
- 适合：如果你**确信**自己就是一个人、一次写一篇、每篇一天内 ship 完
- 不适合：如果你开始**并行处理 5+ 篇 / 订阅者几百人 / 跨设备查状态**

### 选项 B — 推倒重做成完整 app

- ✅ 优点：UI 好看、调度完善、可视化舒服
- ❌ 缺点：重写 5-8 周；skill-first 的 LLM 交互灵活度会下降（表单代替对话）；部署/维护成本骤增
- 为什么不推荐：已经证明 skill-first 能覆盖生产主路径；从头做 app 会把 LLM-native 的灵活性丢掉

### 选项 C — 混合架构（推荐）★

**核心逻辑留在 CLI + skill；加一层薄的本地 dashboard**。类比：

- `git` (CLI) + GitHub/Gitea (web) — CLI 是权威，web 是观察
- `npm` (CLI) + npmjs.com (web) — 同上
- `docker` (CLI) + Docker Desktop (web) — 同上

架构图：

```
┌──────────────────────────────────────────────────────┐
│  Claude Code (skills)    +    本地 dashboard (new)    │
│       │                              │                │
│       │ invokes                      │ reads + triggers│
│       ▼                              ▼                │
│       ┌──────────────────────────────┐                │
│       │      af CLI (authority)      │  ← 唯一权威源   │
│       └──────────┬───────────────────┘                │
│                  │                                    │
│                  ▼                                    │
│           ~/.agentflow/ (state)                       │
└──────────────────────────────────────────────────────┘
                  ▲
                  │ watches
                  │
      ┌───────────────────────┐
      │ agentflow-daemon      │ ← 新：本地后台进程
      │ - scheduled tasks     │   （定时发推/邮件/扫 hotspots）
      │ - webhook receiver    │
      │ - notification        │
      └───────────────────────┘
```

### 3.1 Dashboard 做什么（不做什么）

**做**：
- **全景视图**：所有 hotspots / drafts / tweets / newsletters / publish history 的 table + filter + sort
- **单篇 idea 的跨通道全景**：一个 hotspot → blog → twitter → newsletter 的 lineage 图
- **时间线**：过去 30 天的 publish history（Ghost / LinkedIn / Twitter / Email 四色时间轴）
- **调度面板**：排队的 scheduled 任务
- **触发按钮**：点 "重新跑今日 hotspots" 调用 `af hotspots` 而不是自己写 scan 逻辑
- **Memory 事件流浏览**：events.jsonl 的搜索 + 过滤

**不做**：
- 不在 dashboard 里写内容（那是 skill 的事）
- 不重新实现 LLM 调用（那是 CLI 的事）
- 不做账号系统 / 多用户
- 不做云部署（本地跑在 localhost:4040）

### 3.2 Dashboard 选型

| 选项 | 复杂度 | 适合 |
|---|---|---|
| Next.js + SQLite | 中 | 如果要好看 + 将来扩展 |
| **Vite + React + fs-watch** | **低** | v0.1 推荐，读 `~/.agentflow/` 文件直接显示 |
| Streamlit / Gradio | 极低 | 原型够用但 UI 丑 |
| TUI (Textual / Ink) | 中 | 只想留在终端 |

**推荐 Vite + React**：没有后端（直接读 `~/.agentflow/` 通过一个 tiny express server 做文件代理）、启动快、将来要加功能不被框架绊住。

### 3.3 Daemon 做什么

新增 `agentflow-daemon`（nodejs 或 python 都行），常驻：

- **cron-like 调度**：读 `~/.agentflow/schedule.yaml`，定时触发 `af hotspots` / `af newsletter-send` / `af tweet-publish`
- **webhook receiver**：监听 email bounce / tweet engagement / Ghost webhook
- **通知**：发布失败时 desktop notification + optional email

可以用 `launchd`（macOS）或 `systemd --user`（Linux）托管。Windows 另说。

---

## 4. 推荐路径

### Phase 1（现在 → 下一版）—— 完成 CLI + skill 核心能力

继续 skill-first：
- 补 image（A/B/C 三阶段）
- 补 twitter（2 周 MVP）
- 补 newsletter（2 周 MVP）
- 补 Memory → Default Strategy（v0.5）

这一期**不做 app**。先把"idea 到 4 通道"的链路补齐。

### Phase 2 —— 加 Dashboard（读）

做 Vite + React 本地面板：
- 全景 table
- 时间线
- 单 idea 跨通道视图
- Memory 事件流浏览

只 **读** 不写。所有操作仍走 CLI / skill。

### Phase 3 —— 加 Daemon（调度 + webhook）

- 定时任务
- Resend/Twitter webhook 接收
- 失败通知

### Phase 4 —— Dashboard 获得有限写能力

触发按钮（"立即跑 `af hotspots`"），新建调度条目（"每周一 8 点发 newsletter"）。
仍然**不**允许在 dashboard 里写内容 / 发推 — 那还是 skill 的专属领域。

---

## 5. 本次评估结论（TL;DR）

**现在不需要推倒重做成 app。**

- skill-first 对**生产**来说还是最佳 UX（对话式编辑 > 表单）
- 但对**观察/归档/调度**，需要一个薄 dashboard + daemon

**具体推荐**：

1. **Phase 1 继续 skill-first**，把 image / twitter / newsletter 补全（3 × 2 周 = 6 周）
2. 补齐后再看：如果到时候 in-flight 项目 ≤ 5 个 / 天用不到 5 次 / 通道 ≤ 3 个，**dashboard 可以不做**
3. 如果到时候真的管不过来（状态追踪感觉吃力、跨通道对不齐、定时任务满足不了），再花 2 周上 Phase 2 dashboard
4. daemon 只在有明确定时需求时再做（v0.7+）

**关键原则**：CLI 永远是权威，任何前端（skill / dashboard / 未来的 app）都是视图。不搞两套逻辑。

---

## 6. 判断何时该上 Phase 2 dashboard 的信号

出现以下任一，就是该做 dashboard 的时候：

- [ ] 你开始用 Apple Notes / Notion 自己手工画 "这篇到哪步了" 的表
- [ ] 每天 `af memory-tail` + `ls ~/.agentflow/drafts` 超过 3 次
- [ ] 有一篇 idea 跨 3+ 通道，你记不清哪些发了哪些没发
- [ ] 你想定时发推/邮件但 crontab 已经长到难维护
- [ ] 你换了新设备，发现所有"上下文"要重新找
- [ ] 你开始想给别人看"我的 pipeline"但没有可展示的东西

当前：**都没命中** — Phase 1 继续就好。

---

## 7. 如果真要做 app 的替代评估

如果你**现在就**坚持做 app（不走 Phase 1），建议至少：

1. **不要** 扔掉 `af` CLI。app 内部 shell out 调 `af`
2. **不要** 把 skill 重写成 app 的表单 UI。skill 和 app 并行存在
3. 用 **Electron + SQLite**（而不是 web deploy）保持本地优先
4. 预算 **6-10 周**（app 本身）+ **2 周**（把 skill/CLI 和 app 打通）

但再次强调：**不推荐现在就做**。

---

## 8. 非技术维度的考虑

- **时间投入**：做 dashboard = 2 周不做内容 = 2 周不 ship 文章。如果当下产出比工具更重要，推迟
- **可替代性**：dashboard 的价值 70% 来自"全景视图"，30% 来自"调度"。前者用一个定期 `af report` 打印摘要能顶 60%
- **心智负担**：多一个服务多一个 crash / 重启 / 更新的点

---

## 9. 最小 `af report`（作为 dashboard 替代品的过渡方案）

先不做 dashboard，做一个 `af report` 命令作为每日 / 每周总览：

```
af report [--window 7d]
```

输出：

```
AgentFlow Report — last 7 days

IDEAS:
  ▸ 3 hotspots scanned, 2 explored, 1 dropped
  ▸ 1 topic intent: "MCP server" (3 uses)

CONTENT IN FLIGHT:
  [draft_ready]  hs_20260422...  "MCP 的未解问题"  2 days ago
  [preview]      hs_20260420...  "Claude Code 的子代理"  4 days ago

SHIPPED (7d):
  ✓ blog (ghost)       2
  ✓ twitter threads    1
  ✓ newsletters        0

ROLLBACKS:
  ⤺ 2 Ghost drafts rolled back (smoke tests)

ATTENTION NEEDED:
  ⚠ 1 draft with unresolved [IMAGE:] placeholder
  ⚠ LinkedIn token missing — will skip on next publish
```

这个命令比做 UI 快 10 倍（2 天 vs 2 周），解决 60% 的"我现在到哪了"焦虑。

---

## 10. 最终建议（落盘为动作项）

| 动作 | 何时 | 替代方案 |
|---|---|---|
| 继续 Phase 1 补 image / twitter / newsletter | 现在起 6 周 | — |
| 做 `af report` 作为总览替代 | 任意时点 2 天 | 目前最 ROI 高 |
| 做 dashboard Phase 2 | 信号见 §6 命中再做 | — |
| 做 daemon Phase 3 | 调度需求明确时 | 短期用 crontab 顶 |
| 推倒做 app | 不推荐 | — |
