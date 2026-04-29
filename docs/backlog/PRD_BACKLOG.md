# WF-Backlog: AgentFlow Post-MVP Backlog
> Priority | Effort | Pages | Updated
> P0-P2 | M | Hotspots / Write / Publish / CLI / API | 2026-04-22

## 1. Background

当前 `agentflow-article-publishing` 已完成 v0.1 MVP 主链路：

- D1 热点发现
- D2 骨架生成与正文填充
- D3 多平台预览
- D4 mock 分发
- FastAPI + Next.js 最小 UI

目前系统已经“能跑通”，但还不算“可持续运营”的内容系统。缺口集中在控制层、恢复能力、回流能力与运营面板。

## 2. Current-State Audit & Delta Analysis

| Module | Existing Capability | Delta | Change Magnitude | Evidence |
|---|---|---|---|---|
| Hotspot discovery | 今日热点可读、可开写、可 skip/save | 缺 review bucket / saved queue / skipped queue | M | `backend/api/routes/hotspots.py`, `frontend/src/app/hotspots/page.tsx` |
| Workflow orchestration | `af run-once` 可跑 D1 并交给 UI | 缺统一状态机、队列视图、恢复入口 | M | `backend/agentflow/cli/commands.py` |
| Draft lifecycle | `metadata.json` 有局部状态字段 | 缺统一状态推导与列表接口 | M | `backend/api/routes/dependencies.py`, `backend/api/routes/articles.py` |
| Write workspace | 可 fill、可逐段 edit、可上传图片 | 缺版本历史、显式复检、局部 regenerate | M-L | `frontend/src/app/write/[articleId]/page.tsx` |
| Publish workspace | 可 preview、可 publish、可 force publish | 缺发布历史、失败重试、凭证状态检查 | M | `frontend/src/app/publish/[articleId]/page.tsx`, `backend/api/routes/publish.py` |
| Style learning | D0 API 已存在 | 缺前端入口与 corpus 回流 UI | M | `backend/api/routes/style.py` |
| Source management | 后端读取 sources/config | 缺 Web UI 配置入口 | M | `backend/agentflow/config/sources_loader.py` |

## 3. Dependency Graph & Delivery Strategy

最小可运营路线：

1. 先补 `workflow control plane`
2. 再补 `style learning / publish history`
3. 最后补 `后台任务 / source 管理 / 版本历史`

原因：

- 没有统一状态与队列，后续所有运营能力都缺挂载点
- style learning / publish history 是次一级的产品闭环能力
- 后台任务和版本历史虽重要，但不先于状态面板

## 4. User Story Decomposition

### US-1 Workflow Queue
- 场景：用户想知道“哪些热点待处理、哪些文章写到哪一步了”
- 功能：统一状态流 + 队列列表 + 恢复入口
- 理由：把一次性 demo 流提升为可持续工作流
- Priority: P0

### US-2 Style Learning Console
- 场景：用户想把样本文章导入系统并检查当前 profile
- 功能：上传 sources、触发 learn/recompute、查看 corpus
- 理由：D0 目前只有 API/CLI，缺操作面
- Priority: P1

### US-3 Publish History & Retry
- 场景：用户发布后想追踪结果，并对失败平台单独重试
- 功能：发布历史页、按平台过滤、重试入口
- 理由：分发不是一次性动作
- Priority: P1

### US-4 Review Buckets
- 场景：用户想区分今日热点、稍后处理、已跳过
- 功能：saved / skipped / pending buckets
- 理由：D1 扫描量上来后必须控噪
- Priority: P1

### US-5 Background Jobs
- 场景：fill / preview / publish 耗时变长时，用户不想卡死在请求上
- 功能：task id、轮询、进度、失败恢复
- 理由：真实 LLM / 真实发布时必需
- Priority: P2

### US-6 Source Management
- 场景：用户要维护 RSS / Twitter / HN 源
- 功能：sources 配置页
- 理由：运营维护效率
- Priority: P2

### US-7 Revision History
- 场景：用户编辑后想对比、撤销、回滚
- 功能：版本历史与 diff
- 理由：提升写作安全性
- Priority: P2

## 5. Information Architecture

```text
Hotspots
├── TodayHotspots
├── WorkflowQueue   <- NEW (P0)
├── SavedHotspots   <- P1
└── SkippedHotspots <- P1

Write(articleId)
├── SkeletonSelector
├── SectionEditors
└── DraftStatusBadge <- P0

Publish(articleId)
├── PlatformPreview
├── ImageTodoPanel
└── PublishStatusSummary <- P0

Future
├── StyleConsole <- P1
└── PublishHistory <- P1
```

设计决策：

- Decision 1 assumes `metadata.json` 已经是文章生命周期的最佳 source of truth。If false, 则需引入单独持久化层。
- Decision 2 assumes v0.1 不需要真正异步 job infra。If false, P2 的后台任务需要前置。

## 6. Detailed Requirements

### 6.1 P0 — Workflow Control Plane

#### Data & Presentation Layer

- 新增 backlog/queue 统一视图，至少展示：
  - `article_id`
  - `hotspot_id`
  - `status`
  - `title`
  - `updated_at`
  - `unresolved_image_count`
  - `last_previewed_platforms`
  - `published_platforms`
- Hotspots 页增加“继续处理中的文章”区块
- 状态文案最少包含：
  - `approved`
  - `skeleton_ready`
  - `draft_ready`
  - `preview_ready`
  - `published`

#### Interaction Layer

- 用户可从热点页直接恢复任一 article
- 如果状态 `< draft_ready`，进入写作页
- 如果状态 `preview_ready` 或 `published`，优先进入发布页
- 每次 fill / preview / publish 时自动更新状态

#### Business & API Layer

- 新增文章队列接口
- 后端统一推导状态，而不是前端猜测
- 状态推导优先基于 `metadata.json`
- 发布成功后状态必须收敛为 `published`

### 6.2 P1 — Style Learning Console

Description:
- 提供样本输入、当前 profile 查看、corpus 列表、recompute 入口

### 6.3 P1 — Publish History & Retry

Description:
- 提供发布历史面板与单平台 retry 流程

### 6.4 P1 — Review Buckets

Description:
- saved / skipped / pending buckets 与恢复动作

### 6.5 P2 — Background Jobs

Description:
- 长耗时任务改为 task-based 状态轮询

### 6.6 P2 — Source Management

Description:
- sources.yaml 的 Web UI 配置面板

### 6.7 P2 — Revision History

Description:
- section 级版本历史、diff、rollback

## 7. Iteration Plan

### Phase 1
- P0: Workflow Control Plane

### Phase 2
- P1: Style Learning Console
- P1: Publish History & Retry
- P1: Review Buckets

### Phase 3
- P2: Background Jobs
- P2: Source Management
- P2: Revision History

## 8. Priority Review

### Why P0 is reasonable

P0 只保留一个 Epic：`Workflow Control Plane`。

原因：

1. 它是当前系统最小“控制面”，没有它，其他能力虽然存在，但用户无法可靠恢复与推进流程。
2. 它复用现有 `metadata.json` 与既有 API，不会推翻 MVP 架构。
3. 行业上内容队列与显式状态流本就是工作流工具的第一优先级能力，尤其在 review / publish 解耦时更重要。

### Why other items are not P0

- `Style Learning Console`：重要，但不阻断现有写作/发布链路
- `Publish History & Retry`：对真实运营重要，但当前 mock 主流程已能闭环
- `Review Buckets`：优化效率，不先于统一文章状态面
- `Background Jobs`：当前请求耗时尚可，真实流量上来后再前置
- `Source Management`：配置效率问题，不是链路阻塞点
- `Revision History`：增强安全性，但不是当前最小缺口

结论：当前优先级排序合理，建议直接推进 P0。

## 9. MEMO

🟡 Pending
- `saved/skipped hotspot buckets` 仍未做，放入 P1
- `publish history UI` 仍未做，放入 P1

🔴 Blocking
- 无 P0 阻塞项

## 10. CHECKPOINT

## CHECKPOINT: WF-Backlog @ Gate 2 — 2026-04-22

### Status
- Gate 0 (Audit): ✅ Confirmed
- Gate 1 (IA): ✅ Confirmed
- Gate 2 (Requirements): ✅ Confirmed
- Gate 3 (Prototype): ⬜ Not started

### Confirmed Decisions
- P0 仅包含 `Workflow Control Plane`
- 状态系统以 `metadata.json` 为 source of truth

### Active Assumptions
- `metadata.json` 足以承载当前状态流：confirmed
- 当前无需异步 job infra：pending

### Deliverables Produced
- `docs/backlog/PRD_BACKLOG.md`

### Next Action
- 实现 P0：统一状态流 + 队列 API + Hotspots 页队列入口
