/**
 * Reference: OpenClaw event listener for AgentFlow `review.*_card` events.
 *
 * Drops into an OpenClaw plugin that already has @larksuite/openclaw-lark
 * installed. Implements the missing 3 pieces called out by BEE:
 *
 *   1. POST /agentflow/events listener
 *   2. renderReviewCard() per backend/agentflow/agent_review/templates/lark_review_cards.md
 *   3. Button callback wiring to AgentFlow's /api/commands
 *
 * This file is a STARTING POINT, not a finished product. The render*Card
 * functions below ship minimal-but-spec-compliant card payloads for each
 * event_type; refine the visual layout to match your group's preferences.
 *
 * Required env (set on the OpenClaw process, not AgentFlow):
 *   AGENTFLOW_AGENT_EVENT_AUTH_HEADER  shared secret matching AgentFlow side
 *   AGENTFLOW_BASE_URL                 e.g. http://127.0.0.1:7860
 *   AGENTFLOW_AGENT_BRIDGE_TOKEN       AgentFlow bridge token
 *   LARK_TARGET_CHAT_ID                Lark chat to render cards into
 *   OPENCLAW_LISTENER_PORT             default 7870
 *
 * AgentFlow side must be configured to point at this listener:
 *   AGENTFLOW_AGENT_EVENT_WEBHOOK_URL=http://<openclaw-host>:7870/agentflow/events
 *   AGENTFLOW_AGENT_EVENT_AUTH_HEADER=Bearer <shared-secret>
 *
 * For dangerous commands (write/edit/refill/image/publish) AgentFlow also
 * needs AGENTFLOW_AGENT_BRIDGE_ENABLE_DANGEROUS=true.
 */

import express, { type Request, type Response } from "express";
// Pulled from @larksuite/openclaw-lark. See https://github.com/larksuite/openclaw-lark/blob/main/index.ts
import {
  sendCardFeishu,
  // Also available from the plugin: updateCardFeishu (in-place card refresh),
  // dispatchFeishuPluginInteractiveHandler is what openclaw-lark calls when a
  // user clicks a button; you register your handler via OpenClaw plugin SDK.
} from "@larksuite/openclaw-lark";

// ---------------------------------------------------------------------------
// 1. AgentFlow envelope schema (mirrors agentflow/shared/agent_bridge.py)
// ---------------------------------------------------------------------------

interface AgentFlowEnvelope {
  schema_version: number;
  event_id: string;
  occurred_at: string;
  ingested_at?: string;
  source: "agentflow.review" | "memory" | "publish" | "api" | string;
  event_type: string;
  article_id?: string | null;
  hotspot_id?: string | null;
  payload: Record<string, unknown>;
  source_ref?: Record<string, unknown>;
  correlation_id?: string;
  session_id?: string;
  actor?: { type?: string; id?: string };
}

// ---------------------------------------------------------------------------
// 2. Card rendering — reads lark_review_cards.md and produces Lark
//    interactive card JSON. Each event_type returns a card with the buttons
//    AgentFlow's daemon expects to receive back via /api/commands.
//
//    The contract authoritative source:
//      backend/agentflow/agent_review/templates/lark_review_cards.md
//    Keep this file in sync if you bump card rendering.
// ---------------------------------------------------------------------------

type LarkCard = Record<string, unknown>;

function makeButton(label: string, action: string, articleId: string, payload: object = {}, type: "primary" | "danger" | "default" = "default"): object {
  return {
    tag: "button",
    text: { tag: "plain_text", content: label },
    type,
    value: { action, article_id: articleId, payload },
  };
}

function renderGateA(env: AgentFlowEnvelope): LarkCard {
  const p = env.payload as { short_id?: string; publisher_brand?: string; candidates?: Array<{ title?: string; angle_index?: number; target_series?: string; slot?: number }> };
  const candidates = p.candidates ?? [];
  const articleId = (env.article_id ?? env.hotspot_id ?? "") as string;

  const buttons: object[] = [];
  candidates.forEach((c, i) => {
    buttons.push(makeButton(`起稿 #${i + 1}`, "gate_a_write", articleId, { angle_index: c.angle_index ?? i, target_series: c.target_series, slot: c.slot ?? i }, "primary"));
    buttons.push(makeButton(`详情 #${i + 1}`, "gate_a_expand", articleId));
  });
  buttons.push(makeButton("全拒绝", "gate_a_reject_all", articleId, { batch_path: p.short_id }, "danger"));
  buttons.push(makeButton("推迟", "defer", articleId, { gate: "A", hours: 4 }));

  return {
    header: { title: { tag: "plain_text", content: `🔥 Gate A · 选题（${candidates.length} 候选）— ${p.publisher_brand ?? ""}` } },
    elements: [
      { tag: "div", text: { tag: "lark_md", content: candidates.map((c, i) => `**#${i + 1}** ${c.title ?? "(no title)"}`).join("\n") } },
      { tag: "action", actions: buttons },
    ],
  };
}

function renderGateB(env: AgentFlowEnvelope): LarkCard {
  const p = env.payload as { title?: string; word_count?: number; section_count?: number; compliance_score?: number; draft_excerpt?: string };
  const articleId = env.article_id as string;
  return {
    header: { title: { tag: "plain_text", content: "📝 Gate B · 草稿待审" } },
    elements: [
      { tag: "div", text: { tag: "lark_md", content: `**${p.title ?? articleId}**\n字数 ${p.word_count ?? "?"} · 节数 ${p.section_count ?? "?"} · 合规 ${p.compliance_score ?? "?"}\n\n${(p.draft_excerpt ?? "").slice(0, 600)}` } },
      {
        tag: "action",
        actions: [
          makeButton("✅ 通过", "approve_b", articleId, {}, "primary"),
          makeButton("✏️ 编辑", "gate_b_edit", articleId),
          makeButton("🔁 重写", "gate_b_rewrite", articleId),
          makeButton("refill", "refill", articleId),
          makeButton("❌ 拒绝", "reject_b", articleId, {}, "danger"),
          makeButton("🔍 diff", "gate_b_diff", articleId),
          makeButton("ℹ︎ meta", "view_meta", articleId),
          makeButton("⏸ 推迟", "defer", articleId, { gate: "B", hours: 4 }),
        ],
      },
    ],
  };
}

function renderGateC(env: AgentFlowEnvelope): LarkCard {
  const p = env.payload as { cover_path?: string; placeholders?: number };
  const articleId = env.article_id as string;
  return {
    header: { title: { tag: "plain_text", content: "🖼 Gate C · 配图待审" } },
    elements: [
      { tag: "div", text: { tag: "lark_md", content: `cover: \`${p.cover_path ?? "(none)"}\`\nplaceholders: ${p.placeholders ?? 0}` } },
      {
        tag: "action",
        actions: [
          makeButton("✅ 通过", "gate_c_approve", articleId, {}, "primary"),
          makeButton("⏭ 跳过", "gate_c_skip", articleId),
          makeButton("🔁 重生成", "gate_c_regen", articleId),
          makeButton("🎨 换 logo", "gate_c_relogo", articleId),
          makeButton("🔍 全列表", "gate_c_full", articleId),
          makeButton("⏸ 推迟", "defer", articleId, { gate: "C", hours: 4 }),
        ],
      },
    ],
  };
}

function renderGateD(env: AgentFlowEnvelope): LarkCard {
  const p = env.payload as { available?: string[]; selected?: string[] };
  const articleId = env.article_id as string;
  const available = p.available ?? [];
  const selected = new Set(p.selected ?? []);
  const toggleButtons = available.map((platform) => makeButton(`${selected.has(platform) ? "☑︎" : "☐"} ${platform}`, "gate_d_toggle", articleId, { platform }));
  return {
    header: { title: { tag: "plain_text", content: "📤 Gate D · 渠道选择" } },
    elements: [
      { tag: "div", text: { tag: "lark_md", content: `已选：${[...selected].join(", ") || "(空)"}` } },
      { tag: "action", actions: toggleButtons },
      {
        tag: "action",
        actions: [
          makeButton("全选", "gate_d_select_all", articleId, { platforms: available }),
          makeButton("存为默认", "gate_d_save_default", articleId),
          makeButton("✅ 确认发布", "gate_d_confirm", articleId, {}, "primary"),
          makeButton("取消", "gate_d_cancel", articleId, {}, "danger"),
          makeButton("⏸ 推迟", "defer", articleId, { gate: "D", hours: 4 }),
        ],
      },
    ],
  };
}

function renderProfileSetup(env: AgentFlowEnvelope): LarkCard {
  const p = env.payload as { profile_id?: string; missing_fields?: string[]; reason?: string };
  return {
    header: { title: { tag: "plain_text", content: "🛠 Profile 补全" } },
    elements: [
      { tag: "div", text: { tag: "lark_md", content: `**${p.profile_id}**\n缺：${(p.missing_fields ?? []).join(", ")}\n原因：${p.reason ?? ""}` } },
      // Profile setup uses an OpenClaw-side multi-step flow rather than a single command;
      // collect text via @-bot pending edit and call lark_apply_pending_edit per turn.
      {
        tag: "action",
        actions: [
          makeButton("⏸ 稍后", "defer", "", { gate: "P" }),
        ],
      },
    ],
  };
}

function renderImageGatePicker(env: AgentFlowEnvelope): LarkCard {
  const articleId = env.article_id as string;
  return {
    header: { title: { tag: "plain_text", content: "🎨 图片策略" } },
    elements: [
      { tag: "div", text: { tag: "lark_md", content: "Gate B 已通过，选图片生成策略：" } },
      {
        tag: "action",
        actions: [
          makeButton("仅封面", "image_gate_cover_only", articleId, {}, "primary"),
          makeButton("封面 + 正文", "image_gate_cover_plus_body", articleId),
          makeButton("跳过", "image_gate_skip", articleId, {}, "danger"),
        ],
      },
    ],
  };
}

function renderLockedTakeover(env: AgentFlowEnvelope): LarkCard {
  const articleId = env.article_id as string;
  return {
    header: { title: { tag: "plain_text", content: "🔒 Locked Takeover · 人工接管" } },
    elements: [
      { tag: "div", text: { tag: "lark_md", content: `稿件 ${articleId} 已进入接管态。@bot 后续消息作为新草稿正文。` } },
      {
        tag: "action",
        actions: [
          makeButton("看 critique", "locked_critique", articleId),
          makeButton("接管编辑", "locked_edit", articleId),
          makeButton("弃稿", "locked_give_up", articleId, {}, "danger"),
        ],
      },
    ],
  };
}

function renderReviewCard(env: AgentFlowEnvelope): LarkCard | null {
  switch (env.event_type) {
    case "review.gate_a_card": return renderGateA(env);
    case "review.gate_b_card": return renderGateB(env);
    case "review.gate_c_card": return renderGateC(env);
    case "review.gate_d_card": return renderGateD(env);
    case "review.profile_setup_card": return renderProfileSetup(env);
    case "review.image_gate_picker_card": return renderImageGatePicker(env);
    case "review.locked_takeover_card": return renderLockedTakeover(env);
    default: return null;
  }
}

function renderNotifyCard(env: AgentFlowEnvelope): LarkCard {
  return {
    header: { title: { tag: "plain_text", content: `🔔 ${env.event_type}` } },
    elements: [
      { tag: "div", text: { tag: "lark_md", content: "```\n" + JSON.stringify(env.payload, null, 2).slice(0, 1500) + "\n```" } },
    ],
  };
}

// ---------------------------------------------------------------------------
// 3. HTTP listener
// ---------------------------------------------------------------------------

const AGENTFLOW_AUTH = process.env.AGENTFLOW_AGENT_EVENT_AUTH_HEADER ?? "";
const LARK_CHAT_ID = process.env.LARK_TARGET_CHAT_ID ?? "";
const PORT = Number(process.env.OPENCLAW_LISTENER_PORT ?? 7870);

const app = express();
app.use(express.json({ limit: "1mb" }));

app.post("/agentflow/events", async (req: Request, res: Response) => {
  if (AGENTFLOW_AUTH && req.headers.authorization !== AGENTFLOW_AUTH) {
    return res.status(401).json({ ok: false, error: "auth mismatch" });
  }
  const env = req.body as AgentFlowEnvelope;
  if (!env || !env.event_type) {
    return res.status(400).json({ ok: false, error: "invalid envelope" });
  }

  try {
    if (env.event_type.startsWith("review.")) {
      const card = renderReviewCard(env);
      if (card) await sendCardFeishu(LARK_CHAT_ID, card);
    } else if (env.event_type.startsWith("notify.")) {
      // Broadcast / status only — never treat as review card.
      // notify.hotspots_digest is NOT Gate A; ignore or render plain summary.
      await sendCardFeishu(LARK_CHAT_ID, renderNotifyCard(env));
    } else if (env.event_type === "gate.transition") {
      // Use as state-update signal; refresh existing card or no-op.
      // Real card content always comes from review.*_card.
    } else if (env.event_type.startsWith("agent.command.")) {
      // Spawned command completed/failed. Refresh originating card or post short notice.
      await sendCardFeishu(LARK_CHAT_ID, renderNotifyCard(env));
    }
    return res.status(200).json({ ok: true, event_id: env.event_id });
  } catch (err) {
    console.error("[agentflow/events] render failed", err);
    return res.status(500).json({ ok: false, error: String(err) });
  }
});

// ---------------------------------------------------------------------------
// 4. Button callback → AgentFlow /api/commands
//
// In an openclaw-lark setup, button clicks arrive via
// dispatchFeishuPluginInteractiveHandler. Register a plugin handler that
// receives { value: { action, article_id, payload }, operator } and calls
// this function to forward to AgentFlow.
// ---------------------------------------------------------------------------

const AGENTFLOW_BASE = process.env.AGENTFLOW_BASE_URL ?? "http://127.0.0.1:7860";
const BRIDGE_TOKEN = process.env.AGENTFLOW_AGENT_BRIDGE_TOKEN ?? "";

interface ButtonValue {
  action: string;
  article_id: string;
  payload?: Record<string, unknown>;
}

interface OperatorContext {
  open_id: string;
  name?: string;
  chat_id?: string;
}

export async function forwardButtonToAgentFlow(value: ButtonValue, operator: OperatorContext): Promise<unknown> {
  // Daemon command names are `lark_<action>` for most actions; some actions
  // map directly (e.g. `defer` → `lark_defer`). The rule: prefix with `lark_`
  // unless the action already starts with `lark_`. See
  // docs/openclaw_plugin_integration.md "命令字典" for the canonical 33-command list.
  const command = value.action.startsWith("lark_") ? value.action : `lark_${normalizeAction(value.action)}`;

  const body = {
    request_id: cryptoRandom(),
    command,
    article_id: value.article_id,
    params: {
      ...(value.payload ?? {}),
      operator_open_id: operator.open_id,
      operator_name: operator.name,
      chat_id: operator.chat_id,
    },
  };

  const resp = await fetch(`${AGENTFLOW_BASE}/api/commands`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${BRIDGE_TOKEN}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });

  const json = (await resp.json()) as { ok: boolean; data?: { reply_card?: LarkCard; reply_text?: string; side_effects?: string[] }; error?: string };
  if (!resp.ok) throw new Error(`/api/commands ${resp.status}: ${json.error ?? "unknown"}`);

  // If daemon returned a reply_card, render it (use updateCardFeishu when
  // updating in-place from the originating message_id).
  if (json.data?.reply_card) {
    await sendCardFeishu(LARK_CHAT_ID, json.data.reply_card);
  }
  return json;
}

function normalizeAction(action: string): string {
  // Most actions in card payloads use the short form (`approve_b`, `gate_b_edit`).
  // Daemon expects `lark_gate_b_approve` etc. Map the short forms here.
  const map: Record<string, string> = {
    approve_b: "gate_b_approve",
    reject_b: "gate_b_reject",
    gate_a_write: "gate_a_write",
    gate_a_expand: "gate_a_expand",
    gate_a_reject_all: "gate_a_reject_all",
    gate_b_edit: "gate_b_edit",
    gate_b_rewrite: "gate_b_rewrite",
    gate_b_diff: "gate_b_diff",
    gate_c_approve: "gate_c_approve",
    gate_c_skip: "gate_c_skip",
    gate_c_regen: "gate_c_regen",
    gate_c_relogo: "gate_c_relogo",
    gate_c_full: "gate_c_full",
    gate_d_toggle: "gate_d_toggle",
    gate_d_select_all: "gate_d_select_all",
    gate_d_save_default: "gate_d_save_default",
    gate_d_confirm: "gate_d_confirm",
    gate_d_cancel: "gate_d_cancel",
    gate_d_resume: "gate_d_resume",
    gate_d_extend: "gate_d_extend",
    gate_d_retry: "gate_d_retry",
    image_gate_cover_only: "image_gate_cover_only",
    image_gate_cover_plus_body: "image_gate_cover_plus_body",
    image_gate_skip: "image_gate_skip",
    locked_critique: "locked_critique",
    locked_edit: "locked_edit",
    locked_give_up: "locked_give_up",
    refill: "refill",
    defer: "defer",
    view_meta: "view_meta",
    view_audit: "view_audit",
    takeover: "takeover",
    apply_pending_edit: "apply_pending_edit",
  };
  return map[action] ?? action;
}

function cryptoRandom(): string {
  return `req_${Math.random().toString(36).slice(2, 10)}_${Date.now()}`;
}

// ---------------------------------------------------------------------------
// 5. @bot text → lark_message
//
// Every @bot text message MUST POST to /api/commands as lark_message and the
// returned data.reply_card MUST be rendered verbatim. Never let an LLM
// fabricate the reply — silence is a contract violation.
// ---------------------------------------------------------------------------

export async function forwardAtBotMessageToAgentFlow(text: string, operator: OperatorContext, articleId?: string): Promise<unknown> {
  const body = {
    request_id: cryptoRandom(),
    command: "lark_message",
    params: {
      text,
      operator_open_id: operator.open_id,
      operator_name: operator.name,
      chat_id: operator.chat_id,
      article_id: articleId,
    },
  };
  const resp = await fetch(`${AGENTFLOW_BASE}/api/commands`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${BRIDGE_TOKEN}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });
  const json = (await resp.json()) as { ok: boolean; data?: { reply_card?: LarkCard; reply_text?: string } };
  if (json.data?.reply_card) await sendCardFeishu(LARK_CHAT_ID, json.data.reply_card);
  else if (json.data?.reply_text) {
    // Fall back to text only when daemon explicitly returns no card.
    // Even then, prefer to wrap as a minimal card via sendCardFeishu.
  }
  return json;
}

// ---------------------------------------------------------------------------

if (require.main === module) {
  app.listen(PORT, () => {
    console.log(`[agentflow event listener] listening on :${PORT}`);
    console.log(`  AGENTFLOW_BASE=${AGENTFLOW_BASE}`);
    console.log(`  LARK_TARGET_CHAT_ID=${LARK_CHAT_ID || "(unset)"}`);
    console.log(`  auth header check: ${AGENTFLOW_AUTH ? "ENABLED" : "DISABLED (set AGENTFLOW_AGENT_EVENT_AUTH_HEADER)"}`);
  });
}

export { app, renderReviewCard, renderNotifyCard };
