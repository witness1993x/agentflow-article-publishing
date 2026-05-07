# BlogFlow TG → Lark 迁移规约（Parity + Independence）

Date: 2026-05-07
Owner: lark-parity branch (fork: agentflow-lark-parity)
Status: **Phase 1 COMPLETE · Phase 2 happy-path VERIFIED · See §11 for completion report**

---

## 0. 原则（Why）

1. **TG 是参考蓝本**。模板、按钮、回调、状态流转、处理逻辑这一套是 TG 这边先打磨成熟的，Lark 端必须**功能对等**而非"另起一套"。
2. **Lark 必须独立闭环**。即使把 `TELEGRAM_BOT_TOKEN` 完全去掉、把 `tg_client.py` / `render.py` / TG 模板全删掉，Lark 也能从 D1 hotspots 扫描走到 D3 publish dispatch，全程不假手 TG。
3. **删除 TG 是后置阶段**。先做 parity + independence，达标后才能讨论 TG 退役。本规约只覆盖 phase 1 / phase 2，phase 3 删除单独立项。

---

## 1. 当前真实状态（v1.1.9 头）

### 1.1 已经对齐的部分（不动）

**Gate 主路径（A/B/C/D + locked takeover）回调全部对齐**：state_machine.md 的 28 对 (gate, action) 在 Lark 都有命令对应（见 §2 矩阵）。

**6 张主交互卡**已经在 `lark_review_cards.md` 定义且 OpenClaw 渲染契约清楚：
- `review.gate_a_card`、`review.gate_b_card`、`review.image_gate_picker_card`、`review.gate_c_card`、`review.gate_d_card`、`review.locked_takeover_card`、`review.profile_setup_card`

**emit 层独立性 OK**：`_emit_lark_*` 系列只检查 `AGENTFLOW_LARK_APP_PRIMARY`，不依赖 `chat_id`、不依赖 TG message_id；TG 不在场时 Lark 卡照常发出。

### 1.2 没对齐的部分（要补）

按严重程度排：

- **GAP-S**（Suggestions 整族缺失）：TG 有 `render_suggestion_list` / `render_suggestion_review` + `S:review` / `S:apply` / `S:dismiss` 三个回调；Lark 端**完全没有**——既没有 `review.suggestion_*_card` 事件，也没有 `lark_suggestion_*` 命令。
- **GAP-P2**（Profile 多轮追问）：TG `render_profile_setup_question` 是个多轮 follow-up 形态（每答一题再发下一张），Lark 卡只有"开始补全"按钮，多轮问答**完全交给 OpenClaw 自己实现**——这是 contract gap，daemon 端没产出 question card 事件。
- **GAP-NOTIFY**（发布侧广播卡）：`render_dispatch_preview` / `render_dispatch_summary` / `render_publish_ready` / `render_publish_digest` —— Lark 端走 `notify.*` 事件，但当前 `lark_review_cards.md` 只规定了 review.* 卡片，notify.\* 的渲染契约未文档化，OpenClaw 要么自己脑补要么不渲染。
- **GAP-CHROME**（操作员 slash 命令缺失）：TG 14 个 slash 命令大部分 Lark 没有原生入口。`lark_message` 自由文本意图分类只覆盖了 /help、/audit per-article 等少数。**完全没有**：/status、/list、/published、/scan、/jobs、/skip、/publish-mark、/cancel、/suggestions、/auth-debug。
- **GAP-AUDIT-LIST**：TG `/audit` 既能 per-article 查也能 list 最近事件；Lark `lark_view_audit` 只 per-article。

### 1.3 独立性的隐性牵绊

- `state.py::gate_history` schema 字段是 `tg_chat_id` + `tg_message_id`：Lark 卡走过的轨迹**没地方落库**，审计断流。
- `short_id.attach_message_id(sid, msg_id)` 存的是 TG message_id：Lark 端没有等价 attach（OpenClaw 渲染 Lark 卡后没把 lark_card_id 回填给 daemon）。
- `auth.is_authorized(uid, action=...)` 的 `uid` 是 TG numeric uid：Lark `operator_open_id` 是字符串 `ou_xxx`，目前 Lark 路径**走的是 bridge token + `_ACTION_REQ` 表**，但没有 per-operator 白名单，所有持有 bridge token 的回调一律放行。这是降级，不是平替。
- `triggers.py:30` 顶层无条件 `import tg_client`：Lark-only 部署也加载 TG SDK 模块（无害但碍眼，且会在没装 `requests` 时抓出意外错误）。
- `daemon.py` 主循环以 TG 轮询为骨架，Lark 模式是 "skip TG branch + run sweeps"。Lark-pure 时 `_handle_message` / `_handle_callback` / `_route` 加起来约 1500+ 行死代码。

---

## 2. Parity Matrix

### 2.1 卡片层 / render 函数 ↔ Lark 事件

| TG render 函数 | Lark emit / event | 状态 | 备注 |
|---|---|---|---|
| `render_gate_a` | `_emit_lark_gate_a_card` → `review.gate_a_card` | ✓ | OK |
| `render_profile_setup_card` | `_emit_lark_review_card("review.profile_setup_card", ...)` | ✓ | 进入卡 |
| `render_profile_setup_question` | — | ❌ **GAP-P2** | 多轮追问 |
| `render_suggestion_list` | — | ❌ **GAP-S** | |
| `render_suggestion_review` | — | ❌ **GAP-S** | |
| `render_gate_b` | `_emit_lark_gate_b_card` | ✓ | |
| `render_image_gate_picker` | `_emit_lark_image_picker_card` | ✓ | |
| `render_gate_c` | `_emit_lark_gate_c_card` | ✓ | |
| `render_gate_d` | `_emit_lark_gate_d_card` | ✓ | |
| `render_locked_takeover` | `_emit_lark_locked_takeover_card` | ✓ | |
| `render_dispatch_preview` | `notify.dispatch_preview` | ⚠️ | 文档化 |
| `render_dispatch_summary` | `notify.dispatch_result` | ⚠️ | 文档化 |
| `render_publish_ready` | `notify.publish_ready` | ⚠️ | 文档化 |
| `render_publish_digest` | `notify.hotspots_digest` 等 | ⚠️ | 全套梳理 |

### 2.2 Callback action ↔ lark_\* 命令

| (gate, action) | Lark 命令 | 状态 |
|---|---|---|
| A:write | `lark_gate_a_write` | ✓ |
| A:reject_all | `lark_gate_a_reject_all` | ✓ |
| A:expand | `lark_gate_a_expand` | ✓ |
| A:defer | `lark_defer(gate=A)` | ✓ |
| P:start | OpenClaw-owned profile flow | ✓（外移） |
| P:later | `lark_defer(gate=P)` | ✓ |
| **S:review** | — | ❌ **GAP-S** |
| **S:apply** | — | ❌ **GAP-S** |
| **S:dismiss** | — | ❌ **GAP-S** |
| B:approve | `lark_gate_b_approve` | ✓ |
| B:reject | `lark_gate_b_reject` | ✓ |
| B:rewrite | `lark_gate_b_rewrite` | ✓ |
| B:edit | `lark_gate_b_edit`（+ `lark_apply_pending_edit` follow-up） | ✓ |
| B:diff | `lark_gate_b_diff` | ✓ |
| B:defer | `lark_defer(gate=B)` | ✓ |
| C:approve | `lark_gate_c_approve` | ✓ |
| C:skip | `lark_gate_c_skip` | ✓ |
| C:regen | `lark_gate_c_regen` | ✓ |
| C:relogo | `lark_gate_c_relogo` | ✓ |
| C:full | `lark_gate_c_full` | ✓ |
| C:defer | `lark_defer(gate=C)` | ✓ |
| D:toggle/select_all/save_default/confirm/cancel/resume/extend/retry | `lark_gate_d_*` 一一对应 | ✓ |
| L:critique/edit/give_up | `lark_locked_*` | ✓ |

**Lark 独有补充**（不在 TG）：`lark_apply_pending_edit`、`lark_message`、`lark_takeover`（手动接管，区别于 locked takeover）、`lark_view_audit`、`lark_view_meta`。这些是好事——Lark 利用了卡 + @bot 双通道更顺滑。

### 2.3 Slash 命令 ↔ Lark 自由文本意图

| TG slash | 含义 | Lark 现状 | 行动 |
|---|---|---|---|
| `/start` | 捕获 chat_id + 鉴权初始化 | OpenClaw 自治 | 不需要补 |
| `/help` | 帮助 | `_help_card`（lark_callback.py:2042） | ✓ |
| `/status` | daemon 状态 + 最近活动 | — | ❌ **GAP-CHROME-1** |
| `/list` | 在审稿件列表 | — | ❌ **GAP-CHROME-2** |
| `/published` | 已发列表 + 链接 | — | ❌ **GAP-CHROME-3** |
| `/suggestions` | 自查建议列表 | — | ❌ **GAP-S** |
| `/scan` | 触发 article-hotspots 扫描 | — | ❌ **GAP-CHROME-4** |
| `/jobs` | 在跑的 subprocess 列表 | — | ❌ **GAP-CHROME-5** |
| `/skip <id>` | 跳过指定文章当前 gate | — | ❌ **GAP-CHROME-6** |
| `/defer <id> <h>` | 推迟指定文章 | `lark_defer` 已限定为 per-card | ⚠️ 命令式版本缺 |
| `/publish-mark <id>` | 手工标记已发布 | — | ❌ **GAP-CHROME-7** |
| `/audit [id]` | 审计日志 | `lark_view_audit` per-id only | ⚠️ list 模式缺 |
| `/auth-debug` | 鉴权诊断 | — | ❌ **GAP-CHROME-8** |
| `/cancel <id>` | 取消文章流程 | — | ❌ **GAP-CHROME-9** |

---

## 3. Gap 修补规约

### 3.1 GAP-S — Suggestions 整族（最大缺口）

**TG 形态参考**（render.py:253–331）：
- `render_suggestion_list(suggestions, ...)`：列出最近 self_check / 用户自查命中的 N 条建议，每条带 `S:review:<sid>` 跳转
- `render_suggestion_review(suggestion, ...)`：单条建议详情 + 按钮 `S:apply` / `S:dismiss`

**Lark 实现规约**：

新增两张卡 + 三个命令：

```yaml
review.suggestion_list_card:
  required_fields: [suggestions[]]
  per_item_buttons:
    - label: "审阅"
      command: lark_suggestion_review
      payload: {suggestion_id}

review.suggestion_review_card:
  required_fields: [suggestion_id, article_id, body, source]
  buttons:
    - label: "应用"
      command: lark_suggestion_apply
      payload: {suggestion_id}
    - label: "忽略"
      command: lark_suggestion_dismiss
      payload: {suggestion_id}
    - label: "返回列表"
      command: lark_suggestion_list
```

新增 helpers：
- `triggers.py::_emit_lark_suggestion_list_card(suggestions)`
- `triggers.py::_emit_lark_suggestion_review_card(suggestion)`

新增 callback：
- `lark_callback.py::_handle_suggestion_review`
- `lark_callback.py::_handle_suggestion_apply`
- `lark_callback.py::_handle_suggestion_dismiss`

`web.py::_LARK_COMMANDS` 注册三条新命令（在 `lark_apply_pending_edit` 之后）。

`auth.py::_ACTION_REQ` 新增 `(S, review)`、`(S, apply)`、`(S, dismiss)` → required="review"。

### 3.2 GAP-P2 — Profile 多轮追问

**TG 形态参考**（`render_profile_setup_question` 在收到答案后再发一条带新问题的卡）。

**Lark 实现规约**：

`review.profile_setup_card` 的 schema 扩展为支持"问答推进"模式：

```yaml
review.profile_setup_card:
  required_fields: [profile_id, reason, missing_fields, session_path]
  optional_fields: [current_question, question_index, total_questions]   # NEW
  buttons:
    - label: "开始补全" / "回答"
      command: lark_profile_advance      # NEW (替代 OpenClaw 自治)
      payload: {profile_id, session_path, answer_field: "text"}
    - label: "稍后"
      command: lark_defer
      payload: {gate: "P"}
  input_fields:
    payload.text: 必填（用户答案）
```

新增 callback `_handle_profile_advance(operator, payload)`：
1. 写答案进 `session_path`（profile-session 文件，遵循 memory 里的 schema 提醒：`status="collecting"` + `active_uid` + `active_chat_id`，**对 Lark 用 `operator_open_id` 替代 active_uid**，新增 `active_lark_chat_id` 字段）
2. 计算下一个 missing field
3. 若还有 → 发新一张 `review.profile_setup_card` with `current_question=<下一题>`
4. 若全部填完 → 关闭 session，发 `notify.profile_setup_done` + 推进到 D1

**注意 footgun**（memory 命中）：profile-session schema 当前要求 `status="collecting"` + `active_uid` + `active_chat_id`。Lark 路径需要新字段 `active_open_id` + `active_lark_chat_id` 并保持 `status` 同步，否则 `_handle_profile_advance` 会找不到 active session。**这是必改的 schema 演进**。

### 3.3 GAP-NOTIFY — notify.\* 渲染契约文档化

不需要新代码，需要扩展 `lark_review_cards.md`（建议拆出新文件 `lark_notify_cards.md`）来定义：

```yaml
notify.dispatch_preview:    # 发布前预览
  fields: [article_id, title, platforms[], target_urls[]]
  buttons: []   # 纯通知
  layout: 列表式

notify.dispatch_result:     # 发布完成 summary
  fields: [article_id, title, succeeded[], failed[], retry_command]
  buttons:
    - label: "重试失败"
      command: lark_gate_d_retry
      payload: {article_id, platforms: <failed>}

notify.publish_ready:       # 发布就绪通告
  fields: [article_id, title, medium_paste_url, published_urls[]]
  buttons: []

notify.publish_digest:      # 周期性 digest
  fields: [period, count, top_articles[]]
  buttons: []

notify.hotspots_digest:     # 热点扫描通告（已有）
  ...

notify.profile_setup_done:  # NEW (GAP-P2 配套)
  fields: [profile_id, completed_fields[]]
```

**关键约束**：notify.\* 不得被渲染成 review card，OpenClaw 必须区分 review/notify 两种语义（这条已经在 `lark_review_cards.md` 顶部约束过，但 notify 自己的契约缺失）。

### 3.4 GAP-CHROME — 操作员 slash 命令的 Lark 替代

不在 Lark 端做 slash bot——OpenClaw 没有 native slash 体验。两条路线选一：

**方案 α（推荐）：扩展 `lark_message` 自由文本意图**

`_route_message_intent` (lark_callback.py:2069) 已经在做意图分类。把以下短语注册成 deterministic intent：

| 用户输入（@bot） | 触发 | 输出 |
|---|---|---|
| "状态" / "status" | `_handle_status` (NEW) | `review.status_card`（在审 N 篇 + 最近 5 个事件） |
| "列表" / "list" / "在审" | `_handle_list` (NEW) | `review.article_list_card`（每篇带按钮跳到 Gate B/C/D） |
| "已发" / "published" | `_handle_published` (NEW) | `review.published_list_card` |
| "扫一下" / "scan" / "找选题" | `_handle_scan` (NEW) | `review.scan_kicked_card` + spawn `_spawn_hotspots` |
| "任务" / "jobs" | `_handle_jobs` (NEW) | `review.jobs_card`（subprocess 状态） |
| "跳过 <id>" | `_handle_skip` (NEW) | 状态机推进 + `notify.action_done` |
| "推迟 <id> <h>" | `_handle_defer_text` (NEW) | 复用 `lark_defer` |
| "标记已发 <id>" | `_handle_publish_mark` (NEW) | 状态 → published |
| "取消 <id>" | `_handle_cancel` (NEW) | 状态 → \*_rejected |
| "审计 [id]" | `_handle_audit_list` (NEW) | 已有 `lark_view_audit` per-id；list 模式发 `review.audit_list_card` |
| "鉴权" / "auth" | `_handle_auth_debug` (NEW) | `review.auth_debug_card`（白名单 + 当前 operator） |
| "建议" / "suggestions" | `_handle_suggestion_list_intent` | 复用 GAP-S 的 `_emit_lark_suggestion_list_card` |

每条新增 chrome card 同时新增 `review.*_card` 事件，遵循 `lark_review_cards.md` 现有约定。

**方案 β（不推荐）：在 Lark 群里挂 OpenClaw slash menu**。Lark 的"快捷菜单"对接成本高，且后续每加一条命令都要在 OpenClaw 改菜单配置。维护性差。

→ 默认 α。

### 3.5 GAP-AUDIT-LIST

`lark_view_audit` 当前签名是 per-article。新增一个无 article_id 的入参形态触发 list 模式（最近 N 条事件）。

或拆成两个命令：`lark_view_audit_article(article_id)` + `lark_view_audit_recent(n=20)`。后者更清楚。

---

## 4. 独立性整改（让 Lark 不依赖 TG 也能跑）

| ID | 当前问题 | 整改 | 紧迫度 |
|---|---|---|---|
| **IND-1** | `state.py::gate_history` 写 `tg_chat_id`/`tg_message_id`，Lark 卡轨迹无处落库 | schema 扩展 `lark_chat_id`/`lark_card_id` 字段，两字段并存（向后兼容历史 entry） | 高 |
| **IND-2** | `short_id.attach_message_id` 只接 TG msg_id | 新增 `attach_lark_card(sid, lark_card_id, lark_chat_id)`；OpenClaw 渲染卡片后回调 `/api/commands` 时带上卡 id | 高 |
| **IND-3** | `auth.is_authorized(uid, action)` uid 是 TG int | 新增 `is_authorized_open_id(open_id, action)`；`_ACTION_REQ` 表共用；维护两份白名单（`~/.agentflow/review/auth.json` 已有 → 加 `lark_operators` 段） | 高 |
| **IND-4** | `triggers.py:30` 顶层 import tg_client | 改为函数内 lazy import，且在 `_lark_app_primary()` 短路时彻底跳过 | 中 |
| **IND-5** | `daemon.py` 主循环 TG 中心 | Lark-pure 模式下抽出 `_main_loop_lark()` —— 心跳 + GC + 超时扫描 + deferred repost + scheduled scan + bridge thread。这是后置阶段（先 parity 再瘦身） | 低（phase 3） |
| **IND-6** | profile-session schema 只支持 TG uid/chat_id | 添加 `active_open_id` + `active_lark_chat_id`，`_handle_profile_advance` 用这俩查 active session | 高（GAP-P2 前置） |
| **IND-7** | `_emit_lark_*` 全部 gated by `_lark_app_primary()` | 短期保留（与 v1.1.7 决策一致）；phase 3 整族删除 flag | 低 |

---

## 5. 实施顺序（三阶段）

### Phase 1 · Parity（功能等价）

> 目标：Lark 在 TG 不退场的前提下做到操作员能用 Lark 单端完成所有 TG 能做的事。

| 步骤 | 内容 | 依赖 |
|---|---|---|
| 1.1 | IND-1 / IND-2：state schema + short_id 双字段 | 无 |
| 1.2 | IND-3：auth open_id 白名单 + `lark_operators` 段 | 无 |
| 1.3 | IND-6：profile-session schema 演进 | 1.1 |
| 1.4 | GAP-NOTIFY：notify.\* 渲染契约文档（`lark_notify_cards.md`） | 无 |
| 1.5 | GAP-S：Suggestions 整族（卡 + 命令 + 状态推进） | 1.2 |
| 1.6 | GAP-P2：Profile 多轮追问（含 IND-6 的 schema 落地） | 1.3 |
| 1.7 | GAP-CHROME 方案 α：12 个意图扩展 `_route_message_intent` + 12 张 chrome card | 1.2 |
| 1.8 | GAP-AUDIT-LIST：`_handle_audit_list` | 1.7 |
| 1.9 | 测试：每条新增命令至少 1 个测用例；`test_lark_callback.py` 覆盖率达成 | 全部 |

工作量估：~5 人日。

### Phase 2 · Independence（去 TG 依赖）

> 目标：把 `TELEGRAM_BOT_TOKEN` 从 .env 拿掉，daemon 跑 Lark-only 模式，所有 phase 1 新功能在没有 TG 的情况下端到端走通。

| 步骤 | 内容 | 依赖 |
|---|---|---|
| 2.1 | IND-4：triggers.py lazy import + 短路 | phase 1 |
| 2.2 | 部署 sandbox：deploy bundle 用 `AGENTFLOW_LARK_APP_PRIMARY=true` 不带 TG token，整套 Gate A→D 走通 | 2.1 |
| 2.3 | 加测试 `test_no_tg_runtime.py`：mock 卸载 tg_client 模块，确认所有 emit 路径仍能跑 | 2.1 |
| 2.4 | doctor 命令产出 "lark-pure-ready" 校验项：profile-session 有 lark 字段、auth.json 有 lark_operators、bridge 端口可达、event webhook URL 不指向自己 | 2.1 |

工作量估：~2 人日。

### Phase 3 · Removal（删除 TG，可选）

> 仅当 phase 2 在生产稳定 ≥ 2 周后启动。

执行内容回到上一版规划（删除 tg_client.py / render.py / TG 模板 / `_handle_message` / `_handle_callback` / `_route` / TG 相关 CLI / TG 文档）。

不在本规约里展开。

---

## 6. 验收标准

### Phase 1 验收

- [ ] `lark_review_cards.md` 增补 `review.suggestion_list_card` / `review.suggestion_review_card`，OpenClaw 能渲染
- [ ] `lark_notify_cards.md` 新增，覆盖 5 个 notify.\* 事件
- [ ] `web.py::_LARK_COMMANDS` 注册数从 ~37 → ≥ 50（含 GAP-S 3 条 + GAP-CHROME 12 条 + profile_advance 1 条 + audit_recent 1 条）
- [ ] `_route_message_intent` 至少 12 个新意图，每个都有 deterministic 关键词触发
- [ ] `gate_history` 新 entry 同时含 tg_\* 和 lark_\* 字段
- [ ] auth.json 支持 `lark_operators` 段并通过 `auth.is_authorized_open_id` 校验
- [ ] 全量 pytest 仍 ≥ 197 passed（不掉），新增用例 ≥ 30

### Phase 2 验收

- [ ] 一台 Linux box `.env` 不含 `TELEGRAM_BOT_TOKEN`，`blogflow review-daemon` 起得来
- [ ] 在该 box 上完成一篇文章 D1 hotspots → Gate A → Gate B → image picker → Gate C → Gate D → published 全程，**无任何 TG 卡**，全部 Lark
- [ ] `test_no_tg_runtime.py` 用 `sys.modules["agentflow.agent_review.tg_client"] = None` 后跑 emit/dispatch 套件，全绿
- [ ] `blogflow doctor --fresh` 在无 TG token 环境下输出 OK

---

## 7. 决策点

| ID | 问题 | 默认 | 谁拍 |
|---|---|---|---|
| D-1 | GAP-CHROME 走方案 α (`lark_message` 意图扩展) 还是 β (Lark slash menu)？ | **α** | 用户 |
| D-2 | GAP-P2 多轮追问：daemon 主导（推荐）vs OpenClaw 主导（旧文档约定）？切换 daemon 主导意味着 OpenClaw 端要把 profile-flow 让出来 | **daemon 主导** | 用户 |
| D-3 | auth `lark_operators` 白名单 phase 1 就上 vs phase 2 再上？ | **phase 1**（避免 phase 2 部署时无 auth 防护裸奔） | 用户 |
| D-4 | `gate_history` 字段双轨（tg_\* + lark_\*）vs 抽象成 `surface_*`（单字段含 type）？后者 schema 更干净，但需要历史 entry 迁移 | **双轨**（向后兼容简单） | 用户 |
| D-5 | phase 3 删除是否要做？现在不决，等 phase 2 在生产稳定后再启动决策 | **延后** | 用户 |

---

## 8. 风险

- **R1 · OpenClaw 端要同步动**：每加一张新卡（5 张 chrome card + 2 张 suggestion card + notify\* 5 张）OpenClaw 都要写渲染分支，否则卡到那儿。**缓解**：phase 1 实施前，在 `.cursor/skills/agentflow-open-claw-v2/SKILL.md` 写好新增渲染契约，让 OpenClaw 端有据可依。
- **R2 · `_route_message_intent` 误触**（memory 命中：v1.1.8 修过自由文本意图的 false positive）：新加 12 个意图必须每条都有**确定性关键词集**，不允许 LLM 推断。每条都要单测 ≥ 3 例 positive + 3 例 false-positive guard。
- **R3 · profile-session schema 演进破坏现有写入**（memory 命中：profile-session 字段缺失会让新写入查不到 active session）：phase 1 步骤 1.3 上线前需要写一次性 migration 把现存 session 文件补 `status="collecting"` + `active_open_id=null` 等默认字段。
- **R4 · MarkdownV2 转义复发**（memory 命中：转义类已复发 4 次）：所有新增 chrome card 走 Lark 模板，不走 TG `parse_mode="MarkdownV2"`，**这条 footgun phase 1 内不会触发**。但如果给 chrome 加 TG 同款入口（比如 `/status` slash），新增 TG render 函数时必须 escape 完整。本规约**不在 TG 侧加新 slash**，规避这条。
- **R5 · 双卡共存导致操作员混淆**：phase 2 之前，TG 和 Lark 同时收到所有 review 卡。**缓解**：phase 1 上线时给 deploy 文档加注："Lark-pure 部署在 phase 2 前请保留 TG 关闭（不写 token）以避免双发"。

---

## 9. 文档产物

phase 1 完成后需新增/更新：

- 新增 `docs/flows/LARK_NOTIFY_CARDS.md`（GAP-NOTIFY）
- 新增 `docs/flows/LARK_OPERATOR_INTENTS.md`（GAP-CHROME 意图清单）
- 更新 `templates/lark_review_cards.md` 加 suggestion + profile_advance 段
- 更新 `templates/state_machine.md` 加 lark_chat_id/lark_card_id 字段说明
- 更新 `.cursor/skills/agentflow-open-claw-v2/SKILL.md` 到 v3.0（含全部新卡渲染契约）
- 更新 `CHANGELOG.md` v1.2.0 段

---

## 10. 下一步

等用户对 D-1 ~ D-5 拍板。phase 1 步骤 1.1 + 1.2（IND-1/IND-2/IND-3）是可以**今天就动手**的低风险改造（只增字段、不改既有逻辑），其余等决策后排期。

---

## 11. Phase 1 完工报告（2026-05-07 当日完成）

### 11.1 交付概览

按默认决策（D-1=A 硬 fork / D-2=blogflow / D-3=phase 1 上 lark_operators / D-4=不要 TG 移动 fallback / D-5=phase 3 延后）执行，多 agent 并行 + 串行混合协同完成。

- **fork 仓**：`~/Desktop/experimental/medium&blog_posting_agent/agentflow-lark-parity/`，分支 `lark-parity`，从主仓 v1.1.9 + WIP blogflow rename 起点
- **commit 链**：`3106718` 起点 snapshot → `<wave1>` 基础层 → `<wave2>` gap 实现 → `01d760a` Wave 4 e2e
- **测试基线**：230 → **303** passed（73 个新增测试，0 既有 regression）

### 11.2 Wave 1 · 基础层（4 agent 并行）

| Wave 1 步骤 | 文件 | 测试 |
|---|---|---|
| **IND-1** `gate_history` 双轨 schema | `state.py` + `templates/state_machine.md` + `templates/callback_data_schema.md` | `test_lark_schema_dual_track.py` (5) |
| **IND-2** `short_id.attach_lark_card` | `short_id.py` | 同上 |
| **IND-3** `is_authorized_open_id` fail-closed + `lark_operators` 段 + 4 CLI 命令 | `auth.py` + `cli/review_commands.py` | `test_auth_lark_operators.py` (7) |
| **IND-6** `claim/find/release_active_session_lark` + `migrate_session_schema_v2` | `topic_profile_lifecycle.py` | `test_profile_session_lark_schema.py` (5) |
| **GAP-NOTIFY** notify.\* 渲染契约 | `docs/flows/LARK_NOTIFY_CARDS.md`（新，255 行）+ `lark_review_cards.md` 顶部交叉引用 | (文档，无单测) |

### 11.3 Wave 2 · Gap 实现（4 agent 串行）

每个 agent 受严格 prompt 约束（`is_authorized_open_id` 而非旧 `is_lark_authorized` / 关键词去歧义 / false-positive 守卫）。

| Wave 2 步骤 | 新增触点 | 测试 |
|---|---|---|
| **GAP-S** Suggestions 整族 | 2 张卡 + 4 命令 + `_authorize_or_deny_v2` + `_LARK_ACTION_REQ` 表 + `_SUGGESTION_HANDLERS` 早期路由 | `test_lark_suggestions.py` (8) |
| **GAP-P2** Profile 多轮追问（daemon-driven） | `review.profile_setup_card` 增 question 字段 + `lark_profile_advance` 命令 + `_PROFILE_HANDLERS` 早期路由 + `notify.profile_setup_done` 真实 emit | `test_lark_profile_advance.py` (9) |
| **GAP-CHROME** 12 operator 意图 | `_CHROME_INTENTS` + `_CHROME_VERB_PATTERNS` + 12 chrome handlers + 7 emit helpers + 12 lark_chrome_\* 命令 + `LARK_OPERATOR_INTENTS.md` | `test_lark_chrome_intents.py` (31，含 6 false-positive 守卫) |
| **GAP-AUDIT-LIST** | `review.audit_list_card` + `_handle_view_audit_recent` + `_AUDIT_HANDLERS` 早期路由 + 刷新/仅看失败按钮 | `test_lark_audit_list.py` (7) |

### 11.4 Wave 4 · e2e 验证

`backend/tests/test_e2e_lark_pure.py`（673 行，1 个综合 test）：

模拟操作员 alice (open_id=ou_e2e_alice, actions=[\"\\*\"])，从 D1 hotspot 起，依次：

1. **Gate A** → `lark_gate_a_write` (slot=0) → mocked `_spawn_async` 同步发出 Gate B 卡
2. **Gate B** → `lark_gate_b_approve` → state `draft_approved` → mocked image picker spawn
3. **Image Picker** → `lark_image_gate_cover_only` → state `image_pending_review`
4. **Gate C** → `lark_gate_c_approve` → state `image_approved` → Gate D 卡
5. **Gate D** → `lark_gate_d_toggle("medium")` + `lark_gate_d_confirm` → mocked publish dispatch → state `published`

**TG 独立性 sentinel**：替换 `tg_client.send_message / send_photo / send_document / send_long_text / answer_callback_query / edit_message_reply_markup / edit_message_text / get_me / get_updates` 为 raise-on-call 哨兵。**测试结尾 `tg_violations == []`**——零 TG 调用。

**结论**：Phase 2 happy-path 独立性 **已验证**——`TELEGRAM_BOT_TOKEN` 不存在、`AGENTFLOW_LARK_APP_PRIMARY=true` 时，文章可以全程 D1 → published 走 Lark，零 TG 路径泄漏。

### 11.5 已知遗留（不影响 Phase 1，列入 Phase 2 后续）

| ID | 项 | 说明 |
|---|---|---|
| **L-1** | IND-4 import-time 独立性 | `triggers.py:30` 仍无条件 `import tg_client`。运行时 sentinel 已证明零调用，但 module load 仍依赖 SDK 存在。修法：lazy import to inside-function。被刻意推迟（多函数引用，独立 PR） |
| **L-2** | Profile yaml 实际 mutation | GAP-P2 答案落 `session.collected[]`，未回写 `topic_profiles.yaml`。`build_patch_from_answers` 存在但 key 命名不匹配（`publisher_account.brand` 点路径 vs 友好 slot 名 `brand`）。需补一层翻译 |
| **L-3** | `chrome_defer` 不真正 schedule | TG 的 `_schedule_deferred_repost` 未在 chrome 路径调用，只 ack + 写 audit memory。需 wire 到现有 deferred-repost store |
| **L-4** | 旧 `_authorize_or_deny`（fail-open via `is_lark_authorized`）仍在用 | 现存 ~30 个 lark_callback handler 走旧路径。新增 v2 handler 都用 `_authorize_or_deny_v2`，但**全量迁移**未做。Phase 2 步骤 2.x |
| **L-5** | `blogflow doctor --fresh` 无 TG token 验证 | 在 pytest 范围之外，需要 CLI/Linux box 手验。Phase 2 §6.2 acceptance |

### 11.6 给主仓的合并建议

- 不建议直接 squash merge 进 `agentflow-article-publishing`：fork 当前 73 个新增测试 + 4 个新文件大块写入，PR 会很大。建议**分批 cherry-pick**：先合 Wave 1（基础 schema 演进，影响面最小），再分别合 GAP-S / GAP-P2 / GAP-CHROME / GAP-AUDIT-LIST 四个独立 PR。
- e2e 测试（Wave 4）作为最后一个 PR 合，标志 phase 1 完工。
- L-1 ~ L-5 单独立项进 Phase 2 排期。


