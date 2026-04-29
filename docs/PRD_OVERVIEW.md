# WF-Overview: AgentFlow 内容工作流 PRD
> Priority | Effort | Pages | Updated
> P0-P2 | L | Hotspots / Write / Publish / Style / Rollback | 2026-04-24

## 1. Background

`agentflow-article-publishing` 在 v0.1 MVP 阶段经历了两次形态跃迁：

- **第一次**（Wave 2F/2G/3）：从“热点 → 骨架 → 人工点 fill → draft” 调整为 “热点 → 默认自动生成完整 Draft → 人工局部干预 → 多平台预览与发布”
- **第二次**（本轮）：从 Next.js + FastAPI Web 应用 收敛为 **skill-first + `af` CLI**。原 Web 应用整体归档到 `_legacy/`，不再维护；日常流程完全运行在 Claude Code 里，由 `.claude/skills/` 下 5 个 skill 编排背后的 `af` CLI

当前系统的真实定位：面向单人创作者的本地优先内容操作系统，**以 Claude Code 为唯一 UX**：

- D0 风格学习
- D1 热点发现
- D2 成稿与局部编辑
- D3 多平台适配
- D4 发布 + 回滚
- 5 个 skill（`agentflow` / `-style` / `-hotspots` / `-write` / `-publish`）编排 `af` CLI

同类参考方面，`Typefully` 验证过“统一写作草稿、审核状态、发布入口、多平台改写”是创作者工具的有效主路径。AgentFlow 的差异点：

1. 不做社媒排程器，做**长文生产链路**
2. 不做 SaaS，做**本地优先 + Claude Code 内嵌**
3. 不做 Web UI，做 **skill-first**（无浏览器、无服务进程、无多用户）

## 2. Current-State Audit & Delta Analysis

| Module | Existing Capability | Delta | Change Magnitude | Evidence |
|---|---|---|---|---|
| Style learning (D0) | `af learn-style --dir / --show / --recompute`；已过实 Key（Kimi 分析 samples） | 缺 style 结果对默认写作策略的自动反哺 | M | `backend/agentflow/agent_d0/`, `samples/` |
| Hotspot discovery (D1) | `af hotspots --json`；Twitter+RSS+HN + Jina 聚类 + Kimi 观点挖掘；实 Key 过 | 缺 sources 管理 CLI、定时扫描、采集失败分层 | M | `backend/agentflow/agent_d1/`, `~/.agentflow/sources.yaml` |
| Write workspace (D2) | `af write --auto-pick`、`af fill`、`af edit`；default auto-fill 0/0/0；实 Key 过 | 缺版本历史、局部 regenerate 粒度、Kimi 生成段落长度 overshoot | M-L | `backend/agentflow/agent_d2/`, prompts |
| Platform adapt (D3) | `af preview`；Ghost/LinkedIn/Medium adapter；实 Key 过 | tag 抽取仍靠 heuristic（本轮修过头尾虚词），缺 semantic tag | S | `backend/agentflow/agent_d3/` |
| Publish (D4) | `af publish`；Ghost 实 Key 过、可选 `GHOST_STATUS=draft`；LinkedIn 缺 token | 缺凭证健康预检、publish-before checklist 的代码形式 | M | `backend/agentflow/agent_d4/publishers/` |
| **Rollback（本轮新加）** | `af publish-rollback [--post-id]`；Ghost DELETE + history 追加 + memory 写 `publish_rolled_back` | LinkedIn/Medium API 不支持程序化 delete；暂只覆盖 Ghost | — | `backend/agentflow/cli/commands.py`, `ghost.py:rollback` |
| Unified memory layer | append-only `~/.agentflow/memory/events.jsonl`；9 种事件 | 缺事件消费层、偏好聚合层、默认策略自动调整（roadmap 重点） | M-L | `backend/agentflow/shared/memory.py` |
| Skill 编排层 | 5 个 SKILL.md；`agentflow-publish` 含 rollback + **Step 1b pre-publish overview**（lineage + references + compliance + hallucination flag） | 其余 4 个 skill 未系统审阅本轮 CLI 变化 | S | `.claude/skills/` |
| Runtime persistence | `~/.agentflow/` 本地持久化结构 | 缺多设备同步、权限模型、数据库化 | L | `README.md` |
| Real-key readiness | `.env.template` + `.env` 实际填好；本轮 CLI 加 `_load_dotenv_once()` 自动加载；`GHOST_STATUS` 变量；D0/D1/D2/D3/D4(Ghost) 实 Key 全绿 | LinkedIn OAuth 需用户手动一次性配置；图片支路未在真实文章上触发 | M | `backend/agentflow/cli/commands.py` |

### 审计结论

系统从"能跑通"进入"本地真实可用"：

1. **主路径 mock + 实 Key 都通**（D0–D4 + rollback 全绿，2026-04-24 回归）
2. **安全网落地**：rollback 真实删除 Ghost post，drafts API probe 返回 404
3. **UX 收敛**：Web UI 废弃后，唯一入口是 Claude Code skills；不再维护浏览器/服务进程
4. 当前最大增量**不再**是补页面或基础链路，而是：
   - 把记忆层从"记录行为"升级到"影响默认行为"
   - 把 LinkedIn 实 Key 补齐 + 图片支路打通
   - 规划图片素材如何有策略地进入文章

## 3. Dependency Graph & Delivery Strategy

### 当前已完成的交付基础

1. `af` CLI 12 个子命令全绿（含新增 `publish-rollback`）
2. 5 个 skill 编排层就位；`agentflow-publish` 含完整 Rollback + Step 1b overview
3. 统一记忆层 + publish 历史落地，含 `publish_rolled_back` 事件
4. mock 端到端 + 实 Key 端到端（D0/D1/D2/D3/D4-Ghost）全绿
5. `.env` 自动加载、`GHOST_STATUS` 可切 draft/published

### 下一阶段交付策略

1. **Phase 1: Memory → Default Strategy**
   - 消费 `events.jsonl` 里的 `fill_choices / section_edit / publish` → 输出 `preferences.yaml`
   - 写作/预览/发布前读 preferences 作为默认输入
   - 必须可解释："为什么默认是 X" → 指向具体历史事件
2. **Phase 2: Real-Key Gap Fill**
   - LinkedIn OAuth + 实 Key smoke
   - 图片支路：决定插图时机、图源、placeholder 生成机制
   - 凭证健康检查（skill 层 + CLI 层）
3. **Phase 3: Async & Safety**
   - 后台任务、版本历史
   - rollback 已有基础，扩展到"回退到上一版 draft"
4. **Phase 4: 记忆解释与偏好面板**
   - `af prefs-show` / `af prefs-explain <key>`
   - skill 层可引导用户查看默认策略的历史依据

### 依赖关系

- Memory 消费层 **依赖** 稳定的 `events.jsonl` schema（已稳定）
- 图片支路 **依赖** D2 是否在生成阶段主动插 `[IMAGE:]` placeholder
- Real-key smoke **依赖** 用户本人完成 LinkedIn OAuth（AI 无法代替）

## 4. User Story Decomposition

### US-1 热点到完整稿件（P0，已完成）
- 场景：用户看到热点后，不想先做大量手工选择
- 功能：选中热点后默认直接生成完整 Draft（`af write --auto-pick`）
- 实现：skeleton → fill `0/0/0` 一次性
- 验证：mock + 实 Key 两次

### US-2 局部人工干预（P0，已完成）
- 场景：用户认可大方向，但要局部修改
- 功能：`af edit --section N [--paragraph M] --command "..."`；skeleton 重选后 `af fill`
- 验证：mock 过；实 Key edit 支路未单独 smoke

### US-3 多平台预览与发布（P0，已完成）
- 场景：一稿多发
- 功能：`af preview` → 平台适配 → `af publish`
- 验证：Ghost 实 Key 过（两次 draft 发布 + 两次 rollback 确认 API 404）

### US-4 发布前 overview（本轮新增，P0）
- 场景：发到公共平台前，用户想看一眼稿件来源、质量信号、平台就绪度
- 功能：Step 1b — skill 强制产出 lineage / references / compliance / tag quality / platform readiness；对齐不上 refs 的中心论点打 hallucination 标
- 验证：dry-run 在 `hs_20260423_003-...` 上暴露 "量子纠缠" 标题 vs Vitalik 原推（谈 substrate independence，没提量子纠缠）的不一致

### US-5 撤稿回滚（本轮新增，P0）
- 场景：发出去后发现有问题要撤
- 功能：`af publish-rollback <article_id> [--post-id]`
- 验证：Ghost 真 DELETE，API probe 404 确认，history + memory 事件都落

### US-6 行为记忆沉淀（P0，已完成）
- 场景：用户多次重复同类偏好
- 功能：9 种事件写入 `events.jsonl`
- 状态：记录完成，消费层未启动

### US-7 风格与偏好控制台（P1，待做）
- 场景：用户希望知道当前默认策略从哪来
- 功能（计划）：`af prefs-show`、`af prefs-explain`；skill 可解释

### US-8 实网可运营能力（P1，进行中）
- 场景：准备使用真实 API key 运行
- 功能：凭证检查、source 管理、真实平台 smoke
- 状态：生成 + Ghost 发布已过；LinkedIn + 图片支路待补

### US-9 图片素材策略（本轮新增，P1，待做）
- 场景：博客文章需要配图
- 功能（计划）：D2.5 阶段由 LLM 决定是否需要图；图源可选本地库/生成/抓 reference；D3 按平台规则适配（Ghost 允许、LinkedIn 受限）
- 状态：当前仅有 `af image-resolve` 解绑现有 placeholder，placeholder 本身还未在真实 flow 中生成

### US-10 长耗时与版本历史（P2，待做）
- 场景：长耗时调用、误改回滚
- 功能：后台任务、`af draft-revert`、发布调度

## 5. Information Architecture（skill-first 语义）

```text
Claude Code（唯一入口）
│
├── /agentflow                          （入口 skill：总览 + 路由）
│
├── /agentflow-style                    （周任务）
│   └── 包装 af learn-style
│
├── /agentflow-hotspots                 （每日，选 hotspot + angle）
│   └── 包装 af hotspots / af hotspot-show
│
├── /agentflow-write <hotspot_id>       （每篇一次）
│   ├── 包装 af write --auto-pick
│   ├── 包装 af fill / af edit
│   ├── 包装 af image-resolve
│   └── 包装 af draft-show
│
└── /agentflow-publish <article_id>     （每篇一次）
    ├── Step 1   inspect via af draft-show
    ├── Step 1b  pre-publish overview（lineage + refs + compliance + platforms）★
    ├── Step 2   resolve or strip images
    ├── Step 3   af preview
    ├── Step 4   present platform previews
    ├── Step 5   confirm
    ├── Step 6   af publish
    ├── Step 7   report
    ├── Step 8   close
    └── Rollback  af publish-rollback（按用户请求触发）
```

运行时数据（不变）：

```text
~/.agentflow/
├── style_profile.yaml              D0 产物
├── style_corpus/                   D0 per-article analyses
├── sources.yaml                    D1 配置（KOL/RSS/HN）
├── hotspots/<date>.json            D1 输出
├── drafts/<aid>/                   D2+D3 per-article 目录
│   ├── skeleton.json
│   ├── draft.md
│   ├── metadata.json
│   ├── d3_output.json
│   └── platform_versions/*.md
├── publish_history.jsonl           D4 每次发布/回滚一行
├── memory/events.jsonl             跨篇 append-only 事件
└── logs/{agentflow.log, llm_calls.jsonl}
```

### 设计决策

- Decision 1 — `metadata.json` 继续作为单篇状态 source of truth ✓ 已 confirmed
- Decision 2 — 单人、本地优先 ✓ confirmed
- Decision 3 — 默认自动成稿是唯一主路径 ✓ confirmed
- Decision 4 — `events.jsonl` append-only 足够支撑当前阶段 ✓ confirmed
- Decision 5（本轮）— 无 Web UI、无 HTTP 服务；唯一入口是 Claude Code + `af` CLI ✓ confirmed
- Decision 6（本轮）— rollback 只覆盖 Ghost（v0.1）；LinkedIn/Medium API 层不支持程序化 delete

## 6. Detailed Requirements（skill ↔ CLI 契约）

### 6.1 Feature A — Hotspot Intake

- `af hotspots --json` 输出稳定 schema：`{hotspots: [{id, topic_one_liner, suggested_angles, source_references, recommended_series, freshness_score, depth_potential, ...}]}`
- 日内多次调用复用当天文件（`~/.agentflow/hotspots/<YYYY-MM-DD>.json`）
- `/agentflow-hotspots` skill 不直接写 memory event（只是读取）
- `source_references[]` 必须被下游 Step 1b 能取到

### 6.2 Feature B — Auto Draft

- `af write <hid> --auto-pick --json` 返回 `{article_id, skeleton, draft, auto_filled}`
- 幂等：对同一 hotspot 重跑 write 会生成新 article_id（不覆盖）
- compliance 分数由 per-violation 0.15 扣分（本轮改）；section 的分数是信号，不阻塞
- `af edit` 必须 append `section_edit` 事件；重新 `af fill` 必须 append `fill_choices`

### 6.3 Feature C — Publish

- `af preview` 为每个目标平台生成 `platform_versions/<platform>.md`，YAML front-matter 含 title/tags/formatting_changes
- tags 抽取头尾虚词已剥离（本轮修）；后续可升级为 semantic tag
- `af publish` 默认 Ghost `status=published`；`GHOST_STATUS=draft` 可切（本轮加）
- unresolved images 强制拦截，`--force-strip-images` 才能绕过
- 发布成功必须记 `platform_post_id` 到 history（本轮修）

### 6.4 Feature D — Rollback（新）

- `af publish-rollback <article_id> [--platform ghost_wordpress] [--post-id X]`
- 从 history 查最新 `success` 记录的 `platform_post_id`
- 若历史缺 `platform_post_id`（pre-fix），要求 `--post-id` 显式传
- Ghost DELETE → 204 视为成功 → 写 `publish_rolled_back` + history 加 `status=rolled_back` 行
- 失败兜底：`requests.RequestException` 捕获返回 `(False, reason)`
- `metadata.json` 的 `published_platforms` 移除该平台；若全空则 `status=preview_ready`

### 6.5 Feature E — Pre-Publish Overview（新）

- skill Step 1b 硬性要求（除非用户说 "just publish"）
- 数据来源：`draft-show --json` + `metadata.json` + hotspot 文件 + `platform_versions/*.md` front-matter + env credential 探测
- 展示内容：lineage / topic / content stats / compliance (avg + 最差 section) / tags / images / platforms readiness
- references：top 3-5 条 `source_references`，含 `source / author / url / text_snippet`
- **Hallucination flag**：若文章中心论点未出现在任一 reference 中 → 显式标记

### 6.6 Feature F — Memory & Preferences

- memory 事件流不变（append-only）
- 当前事件：`article_created / fill_choices / section_edit / hotspot_review / preview / publish / publish_rolled_back / learn_style / image_resolved`
- Phase 1 计划：消费层产出 `~/.agentflow/preferences.yaml`；写作/预览/发布前读取

### 6.7 Feature G — Real-Key Readiness

- `MOCK_LLM=true`（默认）下全链路走 fixtures
- `.env` 已含：Moonshot（生成）、Jina（embedding）、Twitter Bearer、Ghost Admin key
- LinkedIn OAuth 需用户手动（30-60min）
- CLI 启动时 `_load_dotenv_once()` 自动从 `backend/.env` 加载（本轮加）

## 7. Iteration Plan

### Phase 0 — 已完成

- `af` CLI 12 子命令 + 5 个 skill
- mock + 实 Key 端到端（D0/D1/D2/D3/D4-Ghost）
- 统一记忆层（9 种事件）
- Rollback（Ghost）
- Step 1b pre-publish overview（含 hallucination flag）
- 4 份文档（README / CC_ONE_PAGE / CCREVIEW_HANDOFF / agentflow-publish SKILL）同步

### Phase 1 — 下一版建议

- Memory 消费层：`preferences.yaml` + `af prefs-show/explain`
- LinkedIn OAuth + 实 Key smoke
- 图片素材策略实现（见 9）
- 其余 4 个 skill 本轮 CLI 变化对齐

### Phase 2 — 稳定性增强

- 长耗时任务化 / 进度反馈
- `af draft-revert` 版本回退
- 发布前 credential health 自动检查（skill 层已有）

### Phase 3 — 更远方向

- 记忆解释面板
- 远程存储/多端同步
- 协作与审核

## 8. MEMO & Confirmation Log

### Confirmed
- skill-first + `af` CLI 是唯一 UX；Next.js+FastAPI 归档到 `_legacy/`
- 默认自动成稿是主路径
- `events.jsonl` 是记忆层唯一载体
- Ghost 是 v0.1 主发布平台；LinkedIn 为可选；Medium deprecated
- Rollback 只覆盖 Ghost
- Step 1b pre-publish overview 必跑（除非 "just publish"）

### Pending (🟡)
- Memory 消费层 / preferences
- LinkedIn OAuth
- 图片素材策略实现
- 其余 4 个 skill 审阅
- `docs/SOLUTION_OVERVIEW.md` 也需同步

### Blocking (🔴)
- 无

## 9. CHECKPOINT

## CHECKPOINT: WF-Overview @ Gate 3 — 2026-04-24

### Status
- Gate 0 (Audit): ✅ Confirmed
- Gate 1 (IA): ✅ Confirmed (skill-first 重排)
- Gate 2 (Requirements): ✅ Confirmed
- Gate 3 (Implementation): ✅ Real-Key Readiness（Ghost 端）completed; LinkedIn 未完成

### Confirmed Decisions
- skill-first + `af` CLI 作为唯一 UX（Decision 5）
- Rollback v0.1 只覆盖 Ghost（Decision 6）
- Step 1b 是发布前必经步骤

### Active Assumptions
- 单人 / 本地优先 / 无 async infra：confirmed
- `events.jsonl` append-only 足够：confirmed
- LinkedIn OAuth 由用户本人完成：confirmed

### Pending Items (🟡)
- Memory → Default Strategy 消费层
- 图片插入流程设计
- LinkedIn OAuth + 实 Key smoke
- 其余 4 个 skill 本轮 CLI 对齐

### Blocking Items (🔴)
- 无

### Deliverables Produced
- `docs/PRD_OVERVIEW.md` （本轮重写）
- `docs/SOLUTION_OVERVIEW.md`（待本轮重写）
- `docs/CC_ONE_PAGE_SUMMARY.md`（本轮已同步）
- `CCREVIEW_HANDOFF.md`（本轮已同步）
- `README.md`（本轮已同步）
- `.claude/skills/agentflow-publish/SKILL.md`（本轮已同步含 Rollback + Step 1b）
- `docs/backlog/PRD_BACKLOG.md`（未在本轮审阅）

### Next Action
- 同步 `docs/SOLUTION_OVERVIEW.md`
- 审阅其余 4 个 skill
- 输出 Memory-to-Default 消费层设计
- 输出图片素材插入策略 MEMO
