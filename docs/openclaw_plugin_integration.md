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

`AGENTFLOW_AGENT_BRIDGE_TOKEN` 在 AgentFlow 端的 `.env` 里（已有）。`AGENTFLOW_AGENT_BRIDGE_ENABLE_DANGEROUS` 不必开 — Lark 命令都标了 `dangerous: false`。

OpenClaw 部署如果与 AgentFlow 同机，URL 用 `http://127.0.0.1:8000`（或你 review-daemon 启动时绑的端口）。跨机部署同时要保证 token 在 OpenClaw 一侧也配好。

## 命令字典（OpenClaw plugin 注册成 tool）

v1.1.1 起，AgentFlow 暴露 **29 个 `lark_*` 命令**，覆盖原 TG 全部 5 个 Gate
(A / B / C / D / L) 的 27 个 callback 动作 + 1 个通用 defer + 1 个 takeover
快捷方式。OpenClaw 插件把这 29 个命令注册成 native tool，agent 收到 Lark
卡片回调或 @-bot 消息时调对应命令。

### Gate A · 选题（开稿前的 hotspot 选择）

| command | scope | params | 行为 |
|---|---|---|---|
| `lark_gate_a_write` | pipeline ⚠️ | `article_id` (= `hotspot_id`), `payload.angle_index`, `payload.target_series` | spawn `af write <hotspot_id> --auto-pick`，立刻返回 "kicked off" 卡，结果通过 event webhook 异步回报 |
| `lark_gate_a_reject_all` | review | `article_id` | 整张 Gate A 卡作废，等下一轮 scan |
| `lark_gate_a_expand` | read | `article_id` | 返回 hotspot 详情卡（mainstream / overlooked / sources），支持卡内展开预览 |

### Gate B · 草稿审稿

| command | scope | params | 行为 |
|---|---|---|---|
| `lark_gate_b_approve` | review | `article_id` | `state.transition` → `draft_approved`，幂等（重复点 → `already_handled`） |
| `lark_gate_b_reject` | review | `article_id` | `state.transition` → `drafting` |
| `lark_gate_b_rewrite` | pipeline ⚠️ | `article_id` | `state.transition` → `drafting` 然后 spawn `af fill --rewrite` |
| `lark_gate_b_edit` | review | `article_id`, `payload.section_index`, `payload.paragraph_index` | 注册 interactive-edit 等待槽，等下一条 @-bot 消息作为修改指令 |
| `lark_gate_b_diff` | read | `article_id` | 返回最新 `d2_structure_audit` verdict + dim_scores + issues 卡片 |

### Gate C · 配图

| command | scope | params | 行为 |
|---|---|---|---|
| `lark_gate_c_approve` | review | `article_id` | `state.transition` → `image_approved` |
| `lark_gate_c_skip` | review | `article_id` | `state.transition` → `image_skipped` |
| `lark_gate_c_regen` | pipeline ⚠️ | `article_id`, `payload.mode` | spawn `af image-gate --mode <mode>`，结果异步 |
| `lark_gate_c_relogo` | pipeline ⚠️ | `article_id` | spawn `af image-gate --logo-only`，结果异步 |
| `lark_gate_c_full` | read | `article_id` | 返回完整 image_placeholders 列表卡片 |

### Gate D · 发布渠道

| command | scope | params | 行为 |
|---|---|---|---|
| `lark_gate_d_toggle` | review | `article_id`, `payload.platform` | 切换单个平台在 metadata `gate_d_selection` 里的勾选 |
| `lark_gate_d_select_all` | review | `article_id`, `payload.platforms` | 一次性选中所有传入平台 |
| `lark_gate_d_save_default` | review | `article_id` | 把当前选择保存到 `~/.agentflow/preferences.json` 作为后续 default |
| `lark_gate_d_confirm` | publish ⚠️ | `article_id` | 当前选择非空时 → `state.transition` → `ready_to_publish` 然后 spawn `af publish --platforms <selection>` |
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
| `lark_refill` | review | `article_id` | **Phase 1 stub**：返回卡片让 operator 去 TG 完成 |
| `lark_defer` | review | `article_id`, `payload.gate` | 任意 Gate 延后决定，无状态变更 |

⚠️ 标记的命令是 **dangerous=true** ，在 AgentFlow 一侧 OpenClaw 进程必须设
`AGENTFLOW_AGENT_BRIDGE_ENABLE_DANGEROUS=true` 才会真正执行；否则
返回 `403`。这些命令会 spawn 子进程，**不阻塞**，立刻返回"kicked off"卡片，
结果以 `agent.command.completed` / `agent.command.failed` 通过 event webhook
回报（见下节）。

非 dangerous 命令在 AgentFlow 端**同步执行**（in-process），响应时间通常 < 50ms。

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

OpenClaw plugin 在收到 AgentFlow 端 `notify_draft_ready` 事件时（v1.1.1 会把这个事件桥接过去；v1.1.0 仍只在 TG / Custom Bot webhook 发），渲染一张 interactive card，按钮的 `value.action` 字段就是上面命令名（去掉 `lark_` 前缀的）：

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
AGENTFLOW_AGENT_EVENT_WEBHOOK_TOKEN=<shared-secret>
```

### Event envelope

每条事件都是一个 JSON：

```json
{
  "schema_version": 1,
  "event_id": "evt_xxxx",
  "occurred_at": "2026-05-06T11:00:00+08:00",
  "ingested_at": "...",
  "source": "gate" | "memory" | "publish" | "api",
  "event_type": "state.transitioned" | "agent.command.completed" | ...,
  "article_id": "...",
  "hotspot_id": "...",
  "payload": { /* event-specific fields */ },
  "actor": {"type": "...", "id": "..."}
}
```

### OpenClaw 该监听的事件类型

| event_type | 触发时机 | OpenClaw 应该做什么 |
|---|---|---|
| `state.transitioned` `payload.to_state == "draft_pending_review"` | Gate B 准备好审稿 | 用插件 `send_card` 把 Gate B 卡片送到目标 Lark 群（按钮 value 用 `gate_b_*` 系列） |
| `state.transitioned` `payload.to_state == "image_pending_review"` | Gate C 配图待审 | 送 Gate C 卡片 |
| `state.transitioned` `payload.to_state == "channel_pending_review"` | Gate D 渠道选择 | 送 Gate D 卡片（多选 toggle 按钮） |
| `state.transitioned` `payload.to_state == "draft_approved"` / `draft_rejected` / `image_approved` / `published` | 终态变化 | 更新原卡片为终态样式，或发简短播报 |
| `agent.command.completed` `payload.command == "hotspots"` | 每日 scan 完成 | 送 Gate A 选题卡 |
| `agent.command.completed` `payload.command == "write"` | 写稿子进程完成 | 更新 Gate A 卡片为 "稿件已生成 → Gate B 即将推送" |
| `agent.command.completed` `payload.command == "image-gate"` | Gate C regen / relogo 完成 | 更新原 Gate C 卡片或发新图预览 |
| `agent.command.completed` `payload.command == "publish"` | Gate D 发布完成 | 发 publish 结果播报 |
| `agent.command.failed` | 任何子进程失败 | 发红色错误卡，附 stderr 摘要 |
| `lark_callback` | 任何 Lark callback 落 memory log（telemetry） | 一般忽略，仅用于审计 |

**关键设计**：state 变化 = OpenClaw 在 Lark 渲新卡的契机。AgentFlow 的
state machine 是单点决策权威，OpenClaw 只是渲染端 + 输入端，**没有第二个状态机**。

### OpenClaw event listener 伪代码

```ts
// pseudo — refer to OpenClaw plugin SDK
import { plugin } from '@larksuite/openclaw-lark';

http.post('/agentflow/events', async (req, res) => {
  if (req.headers['x-agentflow-event-token'] !== process.env.AGENTFLOW_AGENT_EVENT_WEBHOOK_TOKEN) {
    return res.status(401).end();
  }
  const ev = req.body;
  const chatId = process.env.LARK_TARGET_CHAT_ID;

  if (ev.event_type === 'state.transitioned') {
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
  } else if (ev.event_type === 'agent.command.completed' && ev.payload?.command === 'hotspots') {
    await plugin.send_card(chatId, await renderGateACard(ev.article_id));
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
- **Dangerous commands disabled**：v1.1.0 的 `lark_*` 全是 `dangerous: false`，跑命令不需要开 `AGENTFLOW_AGENT_BRIDGE_ENABLE_DANGEROUS`
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
- ✅ **v1.1.1（当前）** — 全 27 个 TG callback 动作 Lark 端 parity（Gate A / B / C / D / L 全覆盖）+ event webhook 文档化
- ⏭ **v1.1.2** — Phase 2 启动 refill 真实写路径（v1.1.0 stub 升级）；OpenClaw 一侧实现 conversational follow-up（@-bot 接 `lark_gate_b_edit` / `lark_locked_edit` 留下的 pending 槽位）
- ⏭ **Phase 2 doc** — 把整篇稿件作为飞书云文档承载（替代 v1.0.30 的截断 + 镜像链接），需 `docx:document` / `drive:drive` 权限
- Phase 2：把整篇稿件作为飞书云文档发到群（替代 v1.0.30 的截断 + 镜像链接）
