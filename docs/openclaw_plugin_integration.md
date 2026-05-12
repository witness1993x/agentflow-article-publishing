# OpenClaw + Lark 集成指南（v1.1.0）

> **目标读者**：在 OpenClaw 上部署 AgentFlow + 接入 Lark/Feishu 的运维操作者
> **前置条件**：飞书自建应用已审核通过（见 `lark_app_permission_checklist.md`）；OpenClaw ≥ `2026.2.26`；Node.js ≥ `v22`

## 架构

```
Lark 群（卡片按钮 / @bot）
        │
        ▼
@larksuite/openclaw-lark   ← 飞书官方插件，npm 装到 OpenClaw
        │
        ▼  (注册为 OpenClaw tool)
OpenClaw agent runtime
        │
        ▼  (HTTP POST 带 token)
AgentFlow agent_review.web  /api/commands
        │
        ▼  (in-process 调用)
agent_review.lark_callback.handle_event(...)
        │
        ▼
review_state.transition / triggers / memory event
```

不要部署本仓库历史版本里的 `lark-adapter/` 目录（已删除）。那个 FastAPI 服务的功能与官方插件重叠 100%，OpenClaw 部署场景下只用官方插件即可。

## 安装步骤

### 1. 装 OpenClaw 官方 Lark 插件

```bash
npm install -g @larksuite/openclaw-lark
```

或在 OpenClaw 的 plugin 配置里加：

```json
{
  "plugins": ["@larksuite/openclaw-lark"]
}
```

确保 OpenClaw 版本至少 `2026.2.26`（`openclaw -v` 查）。

### 2. 配置插件凭证

参考[官方使用指南](https://bytedance.larkoffice.com/docx/MFK7dDFLFoVlOGxWCv5cTXKmnMh)。要填的字段（来自飞书后台已审核通过的自建应用）：

- `app_id`
- `app_secret`
- `verification_token`
- `encrypt_key`（如果飞书后台开了 `加密推送`）

这些字段也在 AgentFlow 的 `~/.agentflow/secrets/.env` 里有一份（`LARK_APP_ID` / `LARK_APP_SECRET` / `LARK_VERIFICATION_TOKEN` / `LARK_ENCRYPT_KEY`）— 同一份凭证，两个进程读各自的副本。

### 3. 配置 OpenClaw → AgentFlow 桥

OpenClaw 插件接到 Lark 卡片回调或群消息后，通过 HTTP POST 调 AgentFlow 的 bridge：

```
POST http://<agentflow-host>:<port>/api/commands
Authorization: Bearer <AGENTFLOW_AGENT_BRIDGE_TOKEN>
Content-Type: application/json

{
  "request_id": "<uuid>",
  "command": "<lark_command_name>",
  "params": { ... }
}
```

`AGENTFLOW_AGENT_BRIDGE_TOKEN` 在 AgentFlow 端的 `.env` 里（已有）。如果要允许 Lark 触发写作、refill、重生成图片、发布等 spawn 子进程动作，OpenClaw 进程需要设置 `AGENTFLOW_AGENT_BRIDGE_ENABLE_DANGEROUS=true`。

Lark App 主路径部署时设置 `AGENTFLOW_LARK_APP_PRIMARY=true`。这会让所有审核卡片走 `review.*_card` 结构化事件，扫描/发布/失败播报走 `notify.*` 事件，统一 POST 到 `AGENTFLOW_AGENT_EVENT_WEBHOOK_URL`，不再依赖旧 Custom Bot。Telegram 仍可同时作为 fallback。

`blogflow review-daemon` 是 Lark-first 的主进程。primary 开启时它会内置启动 agent bridge HTTP API，默认监听 `127.0.0.1:7860`，OpenClaw 卡片 callback 直接调用这个 daemon-owned `/api/commands`，不需要再单独启动 `blogflow review-dashboard`。可用 `AGENTFLOW_REVIEW_BRIDGE_HOST` / `AGENTFLOW_REVIEW_BRIDGE_PORT` 调整监听地址。

Lark 卡片渲染必须按
`backend/agentflow/agent_review/templates/lark_review_cards.md`
执行。该文件是 `renderReviewCard(payload)` 的权威模板，包含每个
`review.*_card` 的必需按钮、输入框字段、payload alias 和 dangerous
要求。`notify.*` 只能渲染为播报，不得伪装成审核卡。
主流程图和场景闭环矩阵见 `docs/flows/LARK_FIRST_REVIEW_FLOWS.md`。

## 命令字典（OpenClaw plugin 注册成 tool）

AgentFlow 当前暴露 **33 个 `lark_*` 命令**，覆盖原 TG 全部 5 个 Gate
(A / B / C / D / L)、通用 defer、takeover、pending edit，以及 Gate B 通过后的
图片策略选择。OpenClaw 插件把这些命令注册成 native tool，agent 收到 Lark
卡片回调或 @-bot 消息时调对应命令。

### Gate A · 选题（开稿前的 hotspot 选择）

| command | scope | params | 行为 |
|---|---|---|---|
| `lark_gate_a_write` | pipeline ⚠️ | `article_id` (= `hotspot_id`), `payload.angle_index`, `payload.target_series` | spawn `blogflow write <hotspot_id> --auto-pick`，立刻返回 "kicked off" 卡，结果通过 event webhook 异步回报 |
| `lark_gate_a_reject_all` | review | `article_id` | 整张 Gate A 卡作废，等下一轮 scan |
| `lark_gate_a_expand` | read | `article_id` | 返回 hotspot 详情卡（mainstream / overlooked / sources），支持卡内展开预览 |

### Gate B · 草稿审稿

| command | scope | params | 行为 |
|---|---|---|---|
| `lark_gate_b_approve` | review | `article_id` | `state.transition` → `draft_approved` + **v1.1.8 自动 spawn image-gate picker 卡到本群**，幂等（重复点 → `already_handled`） |
| `lark_gate_b_reject` | review | `article_id` | `state.transition` → `drafting` |
| `lark_gate_b_rewrite` | pipeline ⚠️ | `article_id` | `state.transition` → `drafting` 然后 spawn `blogflow fill --rewrite` |
| `lark_gate_b_edit` | review ⚠️ | `article_id`, `payload.section_index`, `payload.paragraph_index`, `payload.comment` | 兼容输入框：有 `comment` 时直接 spawn `blogflow edit`；无输入时注册 interactive-edit 等待槽 |
| `lark_gate_b_diff` | read | `article_id` | 返回最新 `d2_structure_audit` verdict + dim_scores + issues 卡片 |

### Gate C · 配图

| command | scope | params | 行为 |
|---|---|---|---|
| `lark_gate_c_approve` | review | `article_id` | `state.transition` → `image_approved` + **v1.1.8 自动 spawn Gate D 卡到本群** |
| `lark_gate_c_skip` | review | `article_id` | `state.transition` → `image_skipped` + **v1.1.8 自动 spawn Gate D 卡到本群** |
| `lark_gate_c_regen` | pipeline ⚠️ | `article_id`, `payload.mode`, `payload.prompt` | 兼容输入框：有 `prompt` 时作为 `--cover-description` 传给 `blogflow image-gate`；结果异步 |
| `lark_gate_c_relogo` | pipeline ⚠️ | `article_id` | spawn `blogflow image-gate --logo-only`，结果异步 |
| `lark_gate_c_full` | read | `article_id` | 返回完整 image_placeholders 列表卡片 |

### Gate D · 发布渠道

| command | scope | params | 行为 |
|---|---|---|---|
| `lark_gate_d_toggle` | review | `article_id`, `payload.platform` | 切换单个平台在 metadata `gate_d_selection` 里的勾选 |
| `lark_gate_d_select_all` | review | `article_id`, `payload.platforms` | 一次性选中所有传入平台 |
| `lark_gate_d_save_default` | review | `article_id` | 把当前选择保存到 `~/.agentflow/preferences.json` 作为后续 default |
| `lark_gate_d_confirm` | publish ⚠️ | `article_id` | 当前选择非空时 → `state.transition` → `ready_to_publish` 然后 spawn `blogflow publish --platforms <selection>` |
| `lark_gate_d_cancel` | review | `article_id` | 清空选择，回退到 `image_approved`，可稍后重新进入 Gate D |
| `lark_gate_d_resume` | review | `article_id` | 调 `triggers.post_gate_d` 重新发卡（兼容老路径，仍发到 TG） |
| `lark_gate_d_extend` | review | `article_id` | 短码 TTL 延长（telemetry only） |
| `lark_gate_d_retry` | publish ⚠️ | `article_id`, `payload.platforms` | 失败后重试 publish |

### L · Locked Takeover（接管态）

| command | scope | params | 行为 |
|---|---|---|---|
| `lark_locked_critique` | read | `article_id` | 返回最新 audit critique 卡片 |
| `lark_locked_edit` | review | `article_id` | 注册手动接管编辑等待槽，等 @-bot 消息作为新草稿正文 |
| `lark_locked_give_up` | review | `article_id` | `state.transition` → `draft_rejected`，文章弃稿 |

### 跨 Gate 通用

| command | scope | params | 行为 |
|---|---|---|---|
| `lark_takeover` | review | `article_id` | 直接进入 manual takeover（fire `triggers.post_locked_takeover`） |
| `lark_view_audit` | read | `article_id` | 返回 audit 历史 |
| `lark_view_meta` | read | `article_id` | 返回 metadata snapshot |
| `lark_refill` | review ⚠️ | `article_id` | `state.transition` → `drafting`，后台 spawn `blogflow fill <article_id> --skeleton-only --auto-pick` |
| `lark_apply_pending_edit` | review ⚠️ | `article_id`, `payload.text` | OpenClaw 收到 @-bot 后续消息后调用；读取并一次性消费最近 `lark_edit_pending` / `lark_locked_edit_pending`，然后 spawn `blogflow edit --post-review` |
| `lark_defer` | review | `article_id`, `payload.gate` | 任意 Gate 延后决定，无状态变更 |
| `lark_message` (v1.1.8) | review | `text`, `operator_open_id`, `operator_name`, `chat_id`, `article_id?` | 自由文本意图路由：通过/驳回/重写/refill/推进 等关键词 → 等价按钮 handler；pending edit 优先；未识别返回结构化帮助卡（永不沉默） |

⚠️ 标记的命令是 **dangerous=true** ，在 AgentFlow 一侧 OpenClaw 进程必须设
`AGENTFLOW_AGENT_BRIDGE_ENABLE_DANGEROUS=true` 才会真正执行；否则
返回 `403`。这些命令会 spawn 子进程，**不阻塞**，立刻返回"kicked off"卡片，
结果以 `agent.command.completed` / `agent.command.failed` 通过 event webhook
回报（见下节）。

非 dangerous 命令在 AgentFlow 端**同步执行**（in-process），响应时间通常 < 50ms。

### v1.1.8 — 自由文本 + 反幻觉

OpenClaw 收到 @-bot 任意文本时**必须**调 `lark_message` 拿 daemon 的结构化回执，
而不是让 LLM 自说自话。Daemon 走确定性关键词分类（CJK 子串 + ASCII 词边界），
未识别一律返回带帮助卡的 `unknown_intent`，已识别就走对应 button handler。

意图矩阵速查：

| 操作员说 | 路由到 | 需要权限 |
|---|---|---|
| `通过` / `approve` / `✅` | `approve_b`（或按当前态走 `推进`） | `review` |
| `驳回` / `拒绝` / `reject` | `reject_b` | `review` |
| `重写` / `rewrite` | `gate_b_rewrite` | `edit` |
| `编辑 ...` / `edit ...` | `gate_b_edit`（文本入 `comment`） | `edit` |
| `refill` / `重新填充` | `refill` | `review` |
| `推进到下个 gate` / `advance` | 按状态：B→`approve_b` / C→`gate_c_approve` / L→`locked_critique` | per target |
| `audit` / `diff` | `gate_b_diff` | `review` |
| `通过封面` / `跳过封面` / `重新生成封面` | `gate_c_*` | `review` / `image` |
| `确认发布` / `取消发布` | `gate_d_confirm` / `gate_d_cancel` | `publish` / `review` |
| 其他 | 结构化帮助卡 | n/a |

**Pending-edit 优先级**：当 `~/.agentflow/memory/events.jsonl` 里存在
该 operator + 当前活跃稿件的 `lark_edit_pending` 事件时，任意非空文本
**直接作为编辑正文**走 `apply_pending_edit`（绕过意图分类），槽位会被
`lark_pending_edit_consumed` 消费一次。

### v1.1.8 — Lark 端逐动作鉴权

平行于 TG 的 `_ACTION_REQ`，按 `open_id` 解析：

- 隐式 operator：env `LARK_OPERATOR_OPEN_ID`（对标 `TELEGRAM_REVIEW_CHAT_ID`），
  自动获得 `["*"]`。
- 白名单文件：`~/.agentflow/review/lark_auth.json`，格式
  `{"authorized_open_ids":[{"open_id":"ou_xxx","name":"...","allowed_actions":["review","edit"]}]}`。
- 未授权命令返回红色 deny 卡，**不触发任何 state mutation**；telemetry
  写入 `outcome=not_authorized`。
- 兼容性：`LARK_OPERATOR_OPEN_ID` 未设 + 白名单空 = 全开（不破坏首装），
  生产部署务必先设环境变量。

## 响应格式

每条命令的成功响应：

```json
{
  "ok": true,
  "request_id": "<echoed>",
  "command": "lark_gate_b_approve",
  "scope": "review",
  "data": {
    "ack": true,
    "reply_card": { /* Lark interactive card payload */ },
    "reply_text": null,
    "side_effects": []
  },
  "stderr": null
}
```

`data.reply_card` 直接交给 OpenClaw 插件的 `send_card` / `update_card` API。`side_effects` 包含 `already_handled` / `unknown_action` / `missing_article_id` 等遥测标签。

幂等失败（重复 approve）走 `data.side_effects: ["already_handled"]`，HTTP 仍是 200，前端展示"该卡片已处理"即可。

## OpenClaw plugin 注册示例

在 OpenClaw 配置里把 6 条 AgentFlow 命令注册成 tool：

```ts
// pseudo-code, refer to OpenClaw plugin SDK docs for actual API
import { registerTool } from '@openclaw/runtime';

const AGENTFLOW_BASE = process.env.AGENTFLOW_BASE_URL ?? 'http://127.0.0.1:8000';
const AGENTFLOW_TOKEN = process.env.AGENTFLOW_AGENT_BRIDGE_TOKEN;

async function callBridge(command: string, params: object) {
  const r = await fetch(`${AGENTFLOW_BASE}/api/commands`, {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${AGENTFLOW_TOKEN}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ command, params }),
  });
  return r.json();
}

registerTool({
  name: 'agentflow.lark_gate_b_approve',
  description: 'Approve an AgentFlow draft at Gate B from a Lark card',
  parameters: {
    type: 'object',
    properties: {
      article_id: { type: 'string' },
      operator_open_id: { type: 'string' },
      operator_name: { type: 'string' },
    },
    required: ['article_id'],
  },
  handler: async (args) => {
    const result = await callBridge('lark_gate_b_approve', args);
    return result.data;
  },
});

// register the other 5 commands the same way
```

## Gate B 卡片内容

OpenClaw plugin 在收到 AgentFlow 端 `review.gate_b_card` 事件（`AGENTFLOW_LARK_APP_PRIMARY=true`）时，按 `templates/lark_review_cards.md` 渲染一张 interactive card。按钮直接使用 payload 里的 `actions[].command` / `actions[].article_id` / `actions[].payload`：

```json
{
  "header": { "title": { "tag": "plain_text", "content": "📝 Gate B · 草稿待审" } },
  "elements": [
    { "tag": "div", "text": { "tag": "lark_md", "content": "..." } },
    {
      "tag": "action",
      "actions": [
        {
          "tag": "button",
          "text": { "tag": "plain_text", "content": "✅ 通过" },
          "type": "primary",
          "value": { "action": "approve_b", "article_id": "<id>" }
        },
        {
          "tag": "button",
          "text": { "tag": "plain_text", "content": "❌ 拒绝" },
          "type": "danger",
          "value": { "action": "reject_b", "article_id": "<id>" }
        },
        {
          "tag": "button",
          "text": { "tag": "plain_text", "content": "🔍 看审计" },
          "value": { "action": "view_audit", "article_id": "<id>" }
        },
        {
          "tag": "button",
          "text": { "tag": "plain_text", "content": "✏️ 提交修改" },
          "value": {
            "action": "gate_b_edit",
            "article_id": "<id>",
            "payload": {"section_index": 2, "comment": "<textarea value>"}
          }
        }
      ]
    }
  ]
}
```

OpenClaw plugin 收到按钮回调，从 `value.action` 拿到 `approve_b`，加上 `lark_` 前缀就是 AgentFlow bridge 命令名 (`lark_gate_b_approve`)，用 `value.article_id` + `event.operator.open_id` 拼参数 POST 出去。

## 事件 webhook（"播报"路径）

> v1.1.1 起 — 这是从 AgentFlow → OpenClaw 的 push 路径，触发 Lark 一侧渲卡。

AgentFlow 已有的 `emit_agent_event` 机制（见 `agentflow/shared/agent_bridge.py`）
会把所有重要事件 POST 到 `AGENTFLOW_AGENT_EVENT_WEBHOOK_URL` 配置的 endpoint。
把这个 endpoint 指到 OpenClaw 的事件接收 URL，OpenClaw agent 收到后
直接调插件的 `send_card` / `send_text` 渲染到对应 Lark 群。

### 配置（AgentFlow `.env`）

```
AGENTFLOW_AGENT_EVENT_WEBHOOK_URL=http://127.0.0.1:<openclaw-listener-port>/agentflow/events
AGENTFLOW_AGENT_EVENT_AUTH_HEADER=Bearer <shared-secret>
```

部署检查：

- `AGENTFLOW_LARK_APP_PRIMARY=true`
- `AGENTFLOW_AGENT_EVENT_WEBHOOK_URL` 指向 OpenClaw event listener
- `AGENTFLOW_AGENT_EVENT_AUTH_HEADER` 与 listener 校验一致
- `blogflow review-daemon` 已启动；daemon 会提供 `http://127.0.0.1:7860/api/commands`
- `AGENTFLOW_AGENT_BRIDGE_ENABLE_DANGEROUS=true` 才能执行写作、编辑、图片生成、发布等按钮
- Lark-first 环境建议清空旧 `LARK_WEBHOOK_URL`，避免继续看到 Custom Bot 摘要卡
- 如果群里仍出现“Gate A 卡已推送到 Telegram / 去 TG 审核”文案，说明线上仍在 legacy Custom Bot 或旧 OpenClaw 渲染路径

### Event envelope

每条事件都是一个 JSON：

```json
{
  "schema_version": 1,
  "event_id": "evt_xxxx",
  "occurred_at": "2026-05-06T11:00:00+08:00",
  "ingested_at": "...",
  "source": "agentflow.review" | "memory" | "publish" | "api",
  "event_type": "review.gate_b_card" | "gate.transition" | "notify.dispatch_result" | ...,
  "article_id": "...",
  "hotspot_id": "...",
  "payload": { /* event-specific fields */ },
  "actor": {"type": "...", "id": "..."}
}
```

### OpenClaw 该监听的事件类型

| event_type | 触发时机 | OpenClaw 应该做什么 |
|---|---|---|
| `review.gate_a_card` | Gate A 文章热点候选待选 | 直接用 payload 渲染可操作选题卡；按钮调用 `lark_gate_a_write` / `lark_gate_a_expand` / `lark_gate_a_reject_all` / `lark_defer` |
| `review.profile_setup_card` | topic profile 缺关键字段 | 送 Lark profile setup 提示卡；后续由 OpenClaw 收集资料并调用 profile/onboard 工具 |
| `review.gate_b_card` | Gate B 稿件待审 | 送稿件审核卡；按钮调用 approve/edit/rewrite/diff/reject/refill/meta/defer |
| `review.image_gate_picker_card` | Gate B 通过后选择是否生成图片 | 送图片策略选择卡；按钮调用 `lark_image_gate_cover_only` / `lark_image_gate_cover_plus_body` / `lark_image_gate_skip` |
| `review.gate_c_card` | Gate C 配图待审 | 送配图审核卡，包含 cover_path；按钮调用 approve/skip/regen/relogo/full/defer |
| `review.gate_d_card` | Gate D 渠道选择 | 送渠道多选卡；按钮调用 toggle/select_all/save_default/confirm/cancel/extend |
| `review.locked_takeover_card` | 重写多轮后进入人工接管 | 送 Locked Takeover 卡；按钮调用 critique/edit/give_up，编辑可继续走 @bot pending edit |
| `gate.transition` `payload.to_state == "draft_pending_review"` | 兼容信号 | 可用于更新状态，但不要再靠它猜卡片内容；以 `review.gate_b_card` 为准 |
| `gate.transition` `payload.to_state == "image_pending_review"` | 兼容信号 | 可用于更新状态，但以 `review.gate_c_card` 为准 |
| `gate.transition` `payload.to_state == "channel_pending_review"` | 兼容信号 | 可用于更新状态，但以 `review.gate_d_card` 为准 |
| `gate.transition` `payload.to_state == "draft_approved"` / `draft_rejected` / `image_approved` / `published` | 终态变化 | 更新原卡片为终态样式，或发简短播报 |
| `agent.command.completed` `payload.command == "article-hotspots"` | 文章热点搜索完成 | 只当完成提示；Gate A 卡片以 `review.gate_a_card` 为准 |
| `agent.command.completed` `payload.command == "write"` | 写稿子进程完成 | 更新 Gate A 卡片为 "稿件已生成 → Gate B 即将推送" |
| `agent.command.completed` `payload.command == "image-gate"` | Gate C regen / relogo 完成 | 更新原 Gate C 卡片或发新图预览 |
| `agent.command.completed` `payload.command == "publish"` | 裸 publish 命令完成 | 只作为命令完成提示；Gate D 结果以 `notify.dispatch_result` 为准 |
| `agent.command.failed` | 任何子进程失败 | 发红色错误卡，附 stderr 摘要 |
| `notify.draft_ready` / `notify.hotspots_digest` / `notify.publish_ready` / `notify.dispatch_result` / `notify.spawn_failure` | `AGENTFLOW_LARK_APP_PRIMARY=true` 时的 Lark-first 通知 | 渲染播报/附件/失败摘要；不要把 `notify.hotspots_digest` 当 Gate A 审核卡 |
| `lark_callback` | 任何 Lark callback 落 memory log（telemetry） | 一般忽略，仅用于审计 |

**关键设计**：state 变化 = OpenClaw 在 Lark 渲新卡的契机。AgentFlow 的
state machine 是单点决策权威，OpenClaw 只是渲染端 + 输入端，**没有第二个状态机**。

### OpenClaw event listener 伪代码

```ts
// pseudo — refer to OpenClaw plugin SDK
import { plugin } from '@larksuite/openclaw-lark';

http.post('/agentflow/events', async (req, res) => {
  if (req.headers.authorization !== process.env.AGENTFLOW_AGENT_EVENT_AUTH_HEADER) {
    return res.status(401).end();
  }
  const ev = req.body;
  const chatId = process.env.LARK_TARGET_CHAT_ID;

  if (ev.event_type?.startsWith('review.')) {
    // renderReviewCard MUST follow
    // backend/agentflow/agent_review/templates/lark_review_cards.md
    await plugin.send_card(chatId, renderReviewCard(ev.payload));
  } else if (ev.event_type === 'gate.transition') {
    const to = ev.payload?.to_state;
    if (to === 'draft_pending_review') {
      await plugin.send_card(chatId, await renderGateBCard(ev.article_id));
    } else if (to === 'image_pending_review') {
      await plugin.send_card(chatId, await renderGateCCard(ev.article_id));
    } else if (to === 'channel_pending_review') {
      await plugin.send_card(chatId, await renderGateDCard(ev.article_id));
    } else if (to === 'published') {
      await plugin.send_text(chatId, `✅ 发布完成 ${ev.article_id}`);
    }
  } else if (ev.event_type?.startsWith('notify.')) {
    await plugin.send_card(chatId, renderNotifyCard(ev.payload));
  }
  res.status(200).end();
});

async function renderGateBCard(articleId) {
  // Pull metadata via /api/article/{articleId}, then build card
  // with buttons whose value carries {action: "approve_b" / "reject_b" / ..., article_id}
}
```

## 安全策略

- **AgentFlow bridge token** 只发给 OpenClaw 进程，不对 Lark 群成员可见
- **Dangerous commands opt-in**：spawn 子进程 / 发布类 `lark_*` 需要 `AGENTFLOW_AGENT_BRIDGE_ENABLE_DANGEROUS=true`；只读和纯 review 命令不需要
- **OpenClaw plugin 群级别 allowlist**：在插件配置里限制 bot 只回应特定群、特定成员，避免外部成员误触发审稿
- **operator 身份链路**：AgentFlow 用 `operator_open_id` 当 `actor=lark:<open_id>` 写状态机历史，事后可审计是谁通过 / 拒绝的

## 故障排查

| 症状 | 诊断 |
|---|---|
| OpenClaw 收到回调但 AgentFlow 没动 | token 错？检查 AgentFlow 日志 `agentflow.agent_review.web` 有没有 401 |
| 重复点击 approve 报错 | 不应该 — 看 `data.side_effects` 是不是 `already_handled` |
| `unknown_action` | OpenClaw 端发的 action 名不在字典里；核对前缀和拼写 |
| `missing_article_id` | 卡片 `value.article_id` 没填或没透传 |
| Lark 收到的卡片是空的 | 看 AgentFlow 响应里 `data.reply_card` 是不是 None — 部分 action（approve / reject）默认不回卡片，让 OpenClaw 自行更新原卡片状态 |

## 版本路线

- ✅ **v1.1.0** — 基础 callback bridge（6 个 lark_* 命令：approve_b / reject_b / takeover / view_audit / view_meta / refill stub）
- ✅ **v1.1.1** — 全 27 个 TG callback 动作 Lark 端 parity（Gate A / B / C / D / L 全覆盖）+ event webhook 文档化
- ✅ **v1.1.2+（当前）** — Lark-first UX：`lark_refill` 真实写路径、输入框 / @bot pending edit 闭环、33 个 `lark_*` 命令、`review.*_card` 审核卡事件、`AGENTFLOW_LARK_APP_PRIMARY=true` 通知迁移
- ⏭ **Phase 2 doc** — 把整篇稿件作为飞书云文档承载（替代 v1.0.30 的截断 + 镜像链接），需 `docx:document` / `drive:drive` 权限
- Phase 2：把整篇稿件作为飞书云文档发到群（替代 v1.0.30 的截断 + 镜像链接）
