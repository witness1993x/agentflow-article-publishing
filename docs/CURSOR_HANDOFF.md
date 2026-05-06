# Cursor / 后续 Agent 接手文档

> 写于 2026-05-06，前任 agent (Claude Code Opus 4.7 / 1M context) 因配额问题交接。
> 这份文档是对话历史摘要 + 项目当前状态 + 已知坑 + v1.1.2 + Phase 2 待办全清单。
> **接手第一步**：把这份文档完整读一遍。

---

## 0. 一句话定位

`agentflow-article-publishing` 是 single-user 写作/发布自动化管线（D1 选题 → D2 写稿 → D3 平台适配 → D4 发布）。HITL 审稿原本走 Telegram；v1.0.30 起加 Lark fan-out；v1.1.0/1.1.1 把 Lark 升到与 TG **同权限的并行主路径**，通过 OpenClaw 官方插件 `@larksuite/openclaw-lark` 接入。

**运营事实**：用户 = 单一运营主体，两端审稿都有同一组人值班。**未来 Lark 会成为主要工作流**，TG 退化为移动/备份。这影响 v1.1.2 优先级：Lark UX 完善 > TG 旧路径维护。

---

## 1. 项目地理

```
~/Desktop/experimental/medium&blog_posting_agent/
├── agentflow-article-publishing/      ← 主仓 (这个目录就是)
│   ├── backend/                       ← Python 包 (agentflow + tests)
│   ├── docs/                          ← 文档
│   ├── scripts/build_deploy_bundle.sh ← 打 framework tarball
│   └── CHANGELOG.md
├── chainstream-service/               ← 兄弟仓 (overlay，单独 repo)
│   ├── overlay/{sources,topic_profile,env}.chainstream.seed.yaml
│   ├── build_bundle.sh                ← 打 chainstream tarball (framework + overlay)
│   └── CHANGELOG.md
└── Twitter_KOL_list.csv               ← 131k 行 KOL 数据集（已被消化进 1.0.5 overlay）
```

**两个 GitHub 远端**：
- `https://github.com/witness1993x/agentflow-article-publishing` (main repo)
- `https://github.com/witness1993x/agentflow-chainstream-service` (overlay)

---

## 2. 当前版本状态 (2026-05-06)

| 包 | 版本 | 包号 | tarball sha256 |
|---|---|---|---|
| `agentflow` (`af` CLI) | v1.1.1 | `pyproject.toml` 写 `1.1.1` | `~/Desktop/agentflow-deploy.tar.gz` = `58670508…fb42a9d` |
| chainstream overlay | 1.0.8 | overlay/CHANGELOG.md | `~/Desktop/agentflow-chainstream-deploy.tar.gz` = `2cc3ba0f…d4fd27ed` |

`af --version` → `1.1.1`

**已合并 PR (按时间顺序)**：
- #1 v1.0.29 D2 结构审计 (commit `bee9754`)
- #2 v1.0.30 Lark 稿件 fan-out (commit `6e8a06a`)
- #3 v1.1.0 OpenClaw 插件桥 (commit `796c0e0`)
- #4 v1.1.1 27-动作 Lark callback parity (commit `9c7dc54`)

主分支 `main` 包含全部 v1.1.1 内容。

---

## 3. 架构核心 — 你必须先理解的 3 件事

### 3.1 状态机是单点权威

`backend/agentflow/agent_review/state.py` 定义全部 review states。所有 Gate (A/B/C/D/L) 和外部触发都通过 `review_state.transition(article_id, gate=, to_state=, actor=, decision=)` 改状态。

**`StateError` 是仲裁机制**：两个 operator 同时点 approve（一个在 TG 一个在 Lark），第一个走通转换、第二个 raise StateError，handler catch 后返回 `side_effects=["already_handled"]`。**绝对不会双重转换**。

不要绕过 `state.transition` 写 `metadata.json`。所有状态变更都该走它。

### 3.2 TG / Lark 双路径，state machine 仲裁

```
              ┌──────────────────────────┐
              │  review_state machine    │ ← 单点决策权威
              └─┬────────────────────┬───┘
                │ transition         │ transition
                ▲                    ▲
   ┌────────────┴────────┐  ┌──────┴────────────┐
   │ daemon.py (TG)      │  │ lark_callback.py  │
   │ 27 callback handlers│  │ 29 lark_* handlers│
   └────────────▲────────┘  └──────▲────────────┘
                │ TG callback      │ HTTP /api/commands
   ┌────────────┴────────┐  ┌──────┴────────────┐
   │ Telegram bot        │  │ OpenClaw + plugin │
   └─────────────────────┘  └───────────────────┘
```

两侧权限完全等价。**actor 字段区分来源**：TG 写 `tg:<chat_id>`，Lark 写 `lark:<open_id>`。

### 3.3 Lark App 模式 (NOT Custom Bot)

v1.0.19 的 `lark_webhook.py` 走 Custom Bot webhook，**push-only，不能接按钮回调**，只用作 fallback 通知。

v1.1.0 起的主路径：
- **出站** (AgentFlow → Lark): 走 OpenClaw 插件 `send_card`，使用 `tenant_access_token` + `LARK_TARGET_CHAT_ID`
- **入站** (Lark → AgentFlow): 走 OpenClaw 插件接到回调 → POST `http://<af-host>/api/commands`，token 从 `AGENTFLOW_AGENT_BRIDGE_TOKEN` 验证
- **播报** (event webhook): AgentFlow `emit_agent_event` POST 到 `AGENTFLOW_AGENT_EVENT_WEBHOOK_URL` (= OpenClaw 监听端) → OpenClaw agent 渲新卡

**不要再加 Custom Bot 调用**。新通知统一走 event webhook 模型。

---

## 4. 关键模块速查

| 路径 | 用途 | 接手时建议先读 |
|---|---|---|
| `backend/agentflow/agent_review/state.py` | 状态机 + transition() | 先 |
| `backend/agentflow/agent_review/lark_callback.py` | 29 lark_* handlers | v1.1.1 主体 |
| `backend/agentflow/agent_review/web.py` | FastAPI bridge `/api/commands` | 改 bridge 时必读 |
| `backend/agentflow/agent_review/triggers.py` | post_gate_a/b/c/d / post_locked_takeover | TG 渲卡入口 |
| `backend/agentflow/agent_review/daemon.py` | TG poll loop + callback dispatch | TG 端等价物 |
| `backend/agentflow/shared/agent_bridge.py` | `emit_agent_event` 出站 webhook | 播报机制 |
| `backend/agentflow/agent_d2/structure_audit.py` | D2 整篇结构审计 (v1.0.29) | D2 改动必读 |
| `backend/agentflow/shared/lark_webhook.py` | Custom Bot 兼容层 (legacy) | 不要再扩 |
| `docs/openclaw_plugin_integration.md` | OpenClaw 插件接入指南 + 29 命令字典 | 部署侧文档 |
| `docs/lark_app_permission_checklist.md` | 飞书自建应用权限申请清单 | 已审批，留作记录 |

---

## 5. 已经踩过的坑（务必读）

### 5.1 PR 合并冲突

各 feature 分支 (#2 / #3 / #4) 都基于较早的 main，每个都改了 CHANGELOG / pyproject / .env.template / test_v02_workflows.py。前一 PR squash-merge 进 main 后，后一 PR 这几个文件会冲突。

**解法**：在 feature 分支上 `git fetch origin main && git rebase origin/main`，手解 4-5 个文件的尾部冲突（通常是把多个新 class 块共存），`git rebase --continue`，`git push --force-with-lease`。然后 squash-merge。

### 5.2 NewsletterCorrectionTests 预存 bug

`tests/test_v02_workflows.py::NewsletterCorrectionTests::test_newsletter_correction_updates_metadata_history_and_memory` 永远失败，是 pre-existing JSON parse bug，与 v1.0.x / v1.1.x 全部无关。**正常 regression 期望 137/138 → 174/175 → ...，剩 1 失败别 panic**。

### 5.3 Worktree 残留

之前用过 isolated worktree (`.claude/worktrees/`) 做并行 agent。残留的话用：
```bash
git worktree remove --force --force .claude/worktrees/<id>
git worktree prune
git branch -D worktree-agent-<id>
rm -rf .claude/worktrees/
```

### 5.4 Force-push 政策

force-push 到 **feature 分支**：可以（rebase 后必须）。
force-push 到 **main**：禁止。永远走 PR + squash merge。
push to main directly：禁止。

### 5.5 .env 改动权限边界

`backend/.env` 含真实 secrets（MOONSHOT_API_KEY / TWITTER_BEARER_TOKEN / 等）。
- **append 新 key 行**：OK（用户授权后）
- **rename key 名**：拒绝过 sed in-place 修改 — 让用户自己改 IDE 里的 3 行
- **读取并展示值**：拒绝 — 永远不要把 secret 内容输出到对话

`backend/.env.template` 是 schema doc，可以随便改。

### 5.6 chainstream overlay 1.0.8 的 4 个 query 重写陷阱

v1.0.7 → 1.0.8 是因为：
- `KYA` 在 Twitter 大小写无关搜里命中 Hindi "kya"（= 什么？）→ 全是印地语 @-reply 闲聊
- `MEV` 命中阿拉伯优惠券里的 "MEv" 字符串
- `MCP` 命中 Anthropic Model Context Protocol，扯进 @karpathy

**模式**：任何 ≤4 字母的 crypto 缩写在 Twitter v2 search 里**必须**和一个 unique-English crypto co-occurrence token 同时出现，否则会被 case-insensitive 前缀匹配带进非英语垃圾。

### 5.7 OpenClaw plugin SDK 我没真验过

`docs/openclaw_plugin_integration.md` 里那段 `registerTool({...})` pseudo-code 是按惯例猜的。**真实部署前**让用户打开飞书云文档（README 里那个 link）核对实际 API 名字，可能要改。

### 5.8 lark-adapter/ 已删除

v1.1.0 早期写过一个 `lark-adapter/` FastAPI 服务用来终止 Lark webhook，**后来删了**（与官方 openclaw-lark 插件功能 100% 重叠）。如果在搜代码时看到 git log 提到，那是已废弃的方向，不要复活。

---

## 6. 命令速查

### 装环境 + 跑测试
```bash
cd backend
python3.11 -m pip install -e . --quiet
python3.11 -m unittest tests.test_v02_workflows tests.test_lark_callback
# expect: 174 tests, 1 failure (NewsletterCorrectionTests pre-existing)
```

### 打 tarball
```bash
cd /Users/witness/Desktop/experimental/medium\&blog_posting_agent/agentflow-article-publishing
bash scripts/build_deploy_bundle.sh
# → ~/Desktop/agentflow-deploy.tar.gz

cd ../chainstream-service
bash build_bundle.sh
# → ~/Desktop/agentflow-chainstream-deploy.tar.gz
```

### 创建 PR (标准流程)
```bash
git checkout -b v1.1.X-<topic>-<branch> main
# ... 改代码 ...
git add <files> && git commit -m "v1.1.X: <topic>

<body>

Co-Authored-By: ..."
git push -u origin v1.1.X-<topic>-<branch>
gh pr create --base main --head v1.1.X-<topic>-<branch> --title "..." --body "..."
# 用户授权后:
gh pr merge <N> --squash --delete-branch
```

### 测试个别 Lark callback
```bash
python3.11 -m unittest tests.test_lark_callback.GateDTests -v
```

---

## 7. 编码规范

- **不写无意义注释**。代码自解释优先；comment 只写 WHY，不写 WHAT。
- **不主动新建 .md 文件**（除非用户明示）— 但 `docs/` 下已有的可以扩。
- **测试**: `unittest`，不要 pytest（项目根没装 pytest dev dep）。Test class 继承 `_AgentflowHomeTestCase` 模式（mock `bootstrap.AGENTFLOW_HOME` 到 tmpdir）。
- **Type hints**: 用，但 `Any` 在 callback payload 这种动态结构里 OK。
- **emit_agent_event**: 加新 event_type 时跟现有 schema 对齐 (`schema_version: 1`, `source`, `event_type`, `article_id`, `payload`, etc.)。
- **idempotency**: 任何会改 state 的操作都 catch StateError，不要让 caller 看到异常。
- **Co-author 行**: commit 末尾加 `Co-Authored-By: <agent name> <noreply@anthropic.com>` 或 cursor 等价物。

---

## 8. 当前未完成清单 (优先级排序)

### v1.1.2 (推荐下一步)

**目标**：让 Lark 真正成为 **主路径** 而不只是 TG 镜像。

#### 8.1 启动 `lark_refill` 真实写路径 (Phase 2)

当前 v1.1.0 的 `lark_refill` 是 stub，回卡让 operator 去 TG。Phase 2 应该真做：
- `_handle_refill` 改为 spawn `af fill --skeleton-only --auto-pick`（mirroring TG 的 I:refill 行为）
- 把 `dangerous: true` 标记加到 `_COMMAND_SPECS["lark_refill"]`
- 测试覆盖：spawn argv 正确、StateError 路径

文件: `backend/agentflow/agent_review/lark_callback.py:209` (_handle_refill), `agent_review/web.py` (spec)

#### 8.2 conversational follow-up for `gate_b_edit` / `locked_edit`

v1.1.1 的 `_handle_gate_b_edit` 和 `_handle_locked_edit` 只是写了一条 `lark_edit_pending` / `lark_locked_edit_pending` memory event 然后返回卡片让 operator 在群里 @-bot。**没有人接 @-bot 消息**。

需要做的：
- OpenClaw plugin 端：监听 `im.message.receive_v1`，拉 article_id 上下文，发回 AgentFlow（新增一个命令 `lark_apply_pending_edit`？）
- AgentFlow 端：加一个 `lark_apply_pending_edit(article_id, edit_text)` 命令
  - 读 `lark_edit_pending` 最近一次 memory event 拿 section_index / paragraph_index
  - 调 `agent_d2.main.apply_user_edit(article_id, section_index, paragraph_index, command=edit_text)`
  - 删除/标记 pending event

#### 8.3 把 `notify_*` 系列从 Custom Bot 迁到 event webhook

`agentflow/shared/lark_webhook.py` 里的：
- `notify_dispatch_result`
- `notify_publish_ready`
- `notify_hotspots_digest`
- `notify_draft_ready` (v1.0.30)
- `notify_spawn_failure`

目前都是直接 POST 到 Custom Bot URL。Lark-first 之后这些应该从 `triggers.py` 调用点改成发 event_webhook，让 OpenClaw 决定怎么渲染。

**渐进做法**：在 triggers.py 调用点 `if AGENTFLOW_LARK_APP_PRIMARY=true: emit_agent_event(... event_type="notify.dispatch_result" ...) else: lark_webhook.notify_dispatch_result(...)`，flag 控制平滑切换。

#### 8.4 TG 通知降级 (可选)

如果 Lark 真的成为主路径，TG 那侧的某些通知就不必发了（避免重复打扰）。考虑：
- 加 `AGENTFLOW_TG_NOTIFY_LEVEL=primary|secondary|silent`，secondary 时 TG 只发 critical (Gate B / spawn failure)，digest 类不发
- 这是 UX 调整不是架构动，等 Lark 部署稳定后再做

### Phase 2 (远期)

#### 8.5 完整稿件作为飞书云文档

v1.0.30 的 `notify_draft_ready` 走截断 + 镜像链接。Phase 2 升级到飞书原生云文档：
- 飞书后台权限：`docx:document` / `drive:drive` (申请清单已写在 docs/lark_app_permission_checklist.md)
- 用 OpenClaw 插件的 `create_doc` API 把 draft.md 转飞书 docx
- 卡片放 doc 链接而不是镜像 URL

### 维护类待办

- 修 NewsletterCorrectionTests 那个 JSON parse bug（不紧急但碍眼）
- 把 v1.1.1 的 `_handle_gate_a_write` 的 `article_id` 实际是 `hotspot_id` 的命名混淆理清（参数名加 hotspot_id 别名）

---

## 9. 部署侧（operator）当前状态

operator 自己在做的：
- `backend/.env` 含真实 secrets（包括 LARK_APP_ID / LARK_APP_SECRET 已配）
- chainstream 的 8 条 default 已 append 进 .env
- OpenClaw 实例理论上有 `LARK_VERIFICATION_TOKEN` / `LARK_TARGET_CHAT_ID`，但需要 rename（之前他写成了 lowercase `verification_token` / `groupchat_id`，要改为大写带 LARK_ 前缀）
- npm install + tool 注册尚未完成

operator 待跑动作：
1. 改 `.env` 3 个 key 名（之前提过的 sed 被拒，让他自己 IDE 改）
2. scp 新 tarball 到 OpenClaw VM 部署
3. `npm install -g @larksuite/openclaw-lark`
4. 配 plugin + 注册 29 个 tool（按 `docs/openclaw_plugin_integration.md`）
5. 实现 `/agentflow/events` 事件接收器（伪代码已给）
6. 端到端冒烟

---

## 10. 给 Cursor 的最小可行动作

如果你刚接手，**第一次会话**建议：
1. 读完这个 doc
2. 跑 `python3.11 -m unittest tests.test_v02_workflows tests.test_lark_callback` 确认环境就绪 (期望 174-1)
3. 读 `agent_review/lark_callback.py` 全文 (约 1500 行)
4. 读 `agent_review/web.py` 的 `_run_lark_command_in_process` (约 line 480)
5. 读 `docs/openclaw_plugin_integration.md` 全文
6. **找用户 confirm**：v1.1.2 优先做 8.1 (refill spawn) 还是 8.2 (conversational follow-up) 还是 8.3 (notify 迁移)

不建议第一次会话就动手大改，先 alignment。

---

## 11. 用户偏好 (从对话历史归纳)

- **中文响应**（用户全程中文）
- **简洁直接**，少 hedge，多结论
- **diff 比一行解释更有用**
- 喜欢看 **sha256 / commit hash / 文件路径**（具体 artifact）
- 不喜欢长 prefix / postfix 寒暄
- 让步要明确说"我之前判断错了"，别绕弯
- **永远先备份 / 给反悔路径**（user 多次强调）
- **让 PR 走 `gh pr merge --squash --delete-branch`**，不直接 push main
- 部署 host 选 **systemd**（与 review-daemon 同构），不用 Docker
- 部署网络选 **Cloudflare Tunnel**（已经讨论过）

---

## 12. 联系链路

- 主仓 PR: https://github.com/witness1993x/agentflow-article-publishing/pulls
- chainstream 仓 PR: https://github.com/witness1993x/agentflow-chainstream-service/pulls
- OpenClaw 官方 Lark 插件: https://github.com/larksuite/openclaw-lark
- 用户邮箱: mobius0083x@gmail.com（CLAUDE.md memory 提供）

---

**Cursor，欢迎接手。这份 doc 应该让你少踩 80% 的坑。剩下 20% 看你了。**
