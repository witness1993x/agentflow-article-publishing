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

| command | scope | params (required) | params (optional) | 行为 |
|---|---|---|---|---|
| `lark_gate_b_approve` | review | `article_id` | `operator_open_id`, `operator_name` | 把 article 状态推到 `draft_approved`；幂等（重复点 → `side_effects: ["already_handled"]`） |
| `lark_gate_b_reject` | review | `article_id` | `operator_open_id`, `operator_name` | 把状态推回 `drafting` |
| `lark_takeover` | review | `article_id` | `operator_open_id` | 触发 `triggers.post_locked_takeover`（发 manual takeover 卡到 TG） |
| `lark_view_audit` | read | `article_id` | — | 返回 `d2_structure_audit` 历史，渲染成 Lark 卡片 payload |
| `lark_view_meta` | read | `article_id` | — | 返回 article metadata snapshot 卡片 |
| `lark_refill` | review | `article_id` | — | **Phase 1 stub**：返回卡片让 operator 去 TG 完成 refill；不真正起子进程 |

所有 `lark_*` 命令在 AgentFlow 端**同步**（in-process），不 spawn 子进程，响应时间通常 < 50ms。

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

## v1.1.1 / v1.1.2 后续路线

- v1.1.1：把 v1.0.30 的 `notify_draft_ready` 升级为通过 OpenClaw plugin 直接发带按钮的 interactive 卡片到 Lark 群（替代 Custom Bot webhook）
- v1.1.2：开通 refill / Gate D / Image Gate 在 Lark 一侧的全部 button-callback 路径
- Phase 2：把整篇稿件作为飞书云文档发到群（替代 v1.0.30 的截断 + 镜像链接）
