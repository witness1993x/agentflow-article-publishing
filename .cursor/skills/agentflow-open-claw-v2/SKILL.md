---
name: agentflow-open-claw-v2
description: AgentFlow article-publishing OpenClaw skill. Default entry is first deployment/onboarding: verify runtime repo, venv, .env, ~/.agentflow, then guide through blogflow bootstrap/onboard/topic-profile/doctor. Use also for Lark-first Gate A/B/C/D review, Telegram fallback, review-daemon, blogflow/mediaflow CLI, image-gate, publish-mark, PR:mark, PD:dispatch, or state-transition work. No runtime source is included.
---

# AgentFlow Open Claw v2.9

## Package Contract

本目录就是可交付给 OpenClaw 的 skill 包：

- `SKILL.md`：触发条件、硬规则、CLI-first 工作流。
- `references/`：长文档、模板、示例，按需读取。
- `assets/`：可作为 `blogflow topic-profile ... --from-file` 参数传入的 YAML 模板。

包内不放 `backend/agentflow/` 源码。OpenClaw/Cursor/Claude Code 是 skill / agent harness；它们加载本 skill 后通过 `blogflow` CLI（`mediaflow` 同入口别名）或 AgentFlow bridge 操作 runtime。Lark-first 部署的业务主进程是 `blogflow review-daemon`：它负责 heartbeat、timeout、定时 article-hotspots，并在 `AGENTFLOW_LARK_APP_PRIMARY=true` 时内置 `/api/commands` bridge。不要再要求单独启动 `blogflow review-dashboard` 来承接 Lark 按钮 callback。

## Default Entry: First Deployment

默认把新会话当作**首次部署 / 初始化续跑**处理，除非 user 明确要求代码修改、review、具体 Gate 排障或已声明 runtime ready。

进来先确认 4 件事：

1. runtime repo 是否存在：`backend/agentflow/`
2. CLI 是否存在：`backend/.venv/bin/blogflow`
3. 凭据文件是否存在：`backend/.env`
4. 用户数据是否存在：`~/.agentflow/`

任一缺失时，不要直接进入 Gate A/B/C/D 或改源码；先引导 `blogflow bootstrap` / `blogflow onboard` / `blogflow topic-profile` / `blogflow doctor`。凭据必须让 user 在终端输入，agent 不接收 key、不手写 `.env`。

## Repo facts

- **Pipeline**: D0 风格 → D1 hotspots → Gate A 选题 → D2 写作 → Gate B 草稿 → D2.5 image → Gate C 封面 → Gate D 渠道 → D3 preview → D4 publish → D4.5 mark/stats
- **State machine**: 14 STATE_* (`backend/agentflow/agent_review/state.py`)。**不是 5 个**——5-state "approved/skeleton/draft/preview/published" 模型已陈旧
- **入口**: `blogflow` CLI（`mediaflow` 同入口别名，旧 `af` 仅 legacy）+ Lark App / OpenClaw bridge (`lark_*` commands, event webhook) + Telegram fallback bot (prefix A/B/C/D + PD/I/L/PR/P/S) + Claude/Cursor skills
- **存储**: `~/.agentflow/` 用户数据；`backend/agentflow/` 框架 (brand-neutral)

## 3 条 hard rules

1. Framework brand-neutral——内容只从 `topic_profiles.yaml` 读，不硬编码品牌
2. 不混 `metadata.json` 与 `events.jsonl`——单文章 state vs 跨文章行为
3. Mock pass ≠ real-key ready——`blogflow doctor` 13 项 probe 才是基准

## Progressive disclosure

- 改动史 → `~/Desktop/agentflow-status.md` (最权威, 7 批)
- TG callback → grep `_route` / `_ACTION_REQ` in `backend/agentflow/agent_review/daemon.py`
- Lark callback → `backend/agentflow/agent_review/lark_callback.py` + `backend/agentflow/agent_review/web.py`
- state 图 → `backend/agentflow/agent_review/templates/state_machine.md`
- Lark 主流程图 → `docs/flows/LARK_FIRST_REVIEW_FLOWS.md`; TG fallback → `docs/flows/TG_BOT_FLOWS.md`; OpenClaw → `docs/openclaw_plugin_integration.md`
- 场景 → `docs/flows/USER_SCENARIOS.md`
- 长参考 → `references/reference.md`

按需读，不要一次全读。

## 验证

- `cd backend && .venv/bin/python -m pytest tests/test_v02_workflows.py -q` (42+ passed)
- `blogflow doctor` 13 probe

## 可选深读

- `references/template.md` 5-段仪式（复杂任务）/ `references/reference.md` 14 STATE 详表 / `references/examples.md` 真实场景

## What this skill is NOT

This skill is **AI 决策上下文**, not runtime. It cannot:

- 起 daemon / 跑 `blogflow doctor` / 执行 `blogflow` CLI
- 创建 `~/.agentflow/` 状态目录
- 自动安装 venv / pip 依赖

To **actually run** AgentFlow you need:

1. **Runtime repo** (`agentflow-framework-{YYYYMMDD}-slim.zip`，含 `backend/agentflow/` 全部 Python 代码 + `pyproject.toml`)
2. **Python venv** (`cd backend && python3 -m venv .venv && pip install -e .`)
3. **`.env`** (mock-only 需 `MOCK_LLM=true`；TG fallback 需 `TELEGRAM_BOT_TOKEN` + `TELEGRAM_REVIEW_CHAT_ID`；Lark-first 需 `AGENTFLOW_LARK_APP_PRIMARY=true`、OpenClaw event webhook、bridge token 和 daemon bridge host/port；real-key 加 LLM/embedding/Atlas 等)
4. **`~/.agentflow/`** 数据目录（首次跑 `blogflow review-init` 自动创建）

Skill 仅在 LLM/agent 思考"在这个 repo 里要做什么"时被加载。**没有 repo，skill 也"无 act 可做"**。

## Required Runtime（依赖检查）

Before this skill provides useful guidance, the following must exist on disk:

| Path | 用途 | 缺失影响 |
|---|---|---|
| `<repo>/backend/agentflow/` | 框架代码 | 完全无法运行；先 unzip slim |
| `<repo>/backend/.venv/bin/blogflow` | CLI 入口 | 任何 `blogflow *` 命令都失败；先 `pip install -e .` |
| `<repo>/backend/.env` | 凭据 | TG/Lark/LLM bridge 不会 connect；先 cp template + 用 onboard/bootstrap 补齐 |
| `~/.agentflow/review/last_heartbeat.json` | daemon 心跳 | `blogflow doctor` 第 13 项报 stale；先 `blogflow review-daemon` 起来 |

如 user 在云端报 "agentflow not found / blogflow command not found / 没找到 ~/.agentflow"，先确认上面 4 项是否齐全。**不要假设 skill 自身能解决 runtime 缺失**。

兼容版本：本 skill v2.9 与 `agentflow-framework-20260428-slim` 及更新版本配合（含 `blogflow-deploy-v1.1.9.tar.gz`）。v2.9 新增"Lark Card Rendering"段，要求 OpenClaw 侧装 `@larksuite/openclaw-lark` 时必须实现 `/agentflow/events` listener，按 `lark_review_cards.md` 渲染 interactive card；纯文本播报扫描结果是契约违反。

## Required Init Flows (MUST follow)

> ⚠ **高权重规则**：触发本 skill 后，**任何 init / credential / profile / skill / cron / daemon 任务都必须走 framework 自带命令**。直接编辑 `.env` / `topic_profiles.yaml` / 手写 plist 会**绕过 framework 的 probe + 验证**，导致 `blogflow doctor` 13 项报错却 silent fail。

| 任务 | MUST 用 | NEVER 做 |
|---|---|---|
| 首次部署 (一站式) | `blogflow bootstrap` | `unzip + sed .env + start daemon` |
| 凭据初始化 / 改 | `blogflow onboard` 或 `blogflow onboard --section <id>` | `echo` / `sed` 填 `.env` |
| 单 skill 安装 | `blogflow skill-install` (含 `--cursor`) | `ln -s` 一条条手装 |
| Profile 初始化 | `blogflow topic-profile init -i --profile <id>` | 手编 `~/.agentflow/topic_profiles.yaml` |
| Profile derive | `blogflow topic-profile derive --profile <id>` | 手填 keyword_groups / do / dont |
| Style 导入 | `blogflow learn-from-handle <handle> --profile <id>` | 手编 `~/.agentflow/style_profile.yaml` |
| TG bot 首次 chat_id 绑定 | TG 发 `/start`（daemon 自动 capture） | 手编 `~/.agentflow/review/config.json` |
| Lark App 主路径 | 配 `AGENTFLOW_LARK_APP_PRIMARY=true`、`AGENTFLOW_AGENT_EVENT_WEBHOOK_URL` 指向 OpenClaw、`AGENTFLOW_AGENT_BRIDGE_TOKEN`、`AGENTFLOW_REVIEW_BRIDGE_PORT`；写操作需 `AGENTFLOW_AGENT_BRIDGE_ENABLE_DANGEROUS=true`；启动 `blogflow review-daemon` | 继续扩 Custom Bot webhook、把 Lark 当 TG 镜像、或另起 `review-dashboard` 当主 callback |
| Daemon 启动 | `blogflow review-daemon` (前台 / systemd / launchd) | `python -m agentflow.cli.commands review-daemon` |
| Cron 定时 | `blogflow review-cron-install --times "..."` | 手写 launchd plist / systemd timer unit |
| 健康度自检 | `blogflow doctor` (13 probe + cache) | grep `~/.agentflow/review/last_heartbeat.json` 自己写逻辑 |

**If user / cloud agent 跳过 init 步骤直接进 runtime**（如 sed `.env`），skill 应**主动指出**："你跳过了 framework 自带的 init 路径；先回去跑 `blogflow bootstrap` 或 `blogflow onboard` 再继续。" 不要默认放行。

## Lark Card Rendering（`@larksuite/openclaw-lark` 已装时强制走的路径）

如果当前 OpenClaw 实例装了官方 `@larksuite/openclaw-lark`（`openclaw plugin ls` 能看到 `openclaw-lark`），那 Gate A/B/C/D 推群**只有一条合法链路**：

```
AgentFlow daemon  ──(POST event envelope)──►  OpenClaw event listener
                                                     │
                                                     ▼  按 lark_review_cards.md 渲染
                                              sendCardFeishu / updateCardFeishu
                                                     │
                                                     ▼ Lark 群（带按钮的 interactive card）
                                                     │ 用户点按
                                                     ▼  dispatchFeishuPluginInteractiveHandler
                                              POST /api/commands  {command, article_id, params}
```

**绝对禁止把扫描结果手撸成纯文本推群**。看到 422 不是契约不通，是配错端：`AGENTFLOW_AGENT_EVENT_WEBHOOK_URL` 必须指向 OpenClaw 自己的 event listener，**不是** AgentFlow 的 `/api/commands`。两端格式不同：
- `AGENTFLOW_AGENT_EVENT_WEBHOOK_URL` 收到的是 **event envelope**（`{schema_version, event_type, payload, article_id, ...}`，见 `agentflow/shared/agent_bridge.py:emit_agent_event`）。
- `/api/commands` 只认 **command 格式**（`{command, article_id, params}`），把 envelope POST 过去必返 422。

### 实现 checklist（OpenClaw 侧 BEE / Cursor 必做）

1. **暴露 listener**：在 OpenClaw 进程里挂一个 HTTP 路由 `POST /agentflow/events`，校验 `Authorization` header == `AGENTFLOW_AGENT_EVENT_AUTH_HEADER`，然后按 `event_type` 分派。
2. **渲卡**：`event_type.startsWith("review.")` 的事件**逐字按** `backend/agentflow/agent_review/templates/lark_review_cards.md` 渲（Gate A/B/C/D + `review.profile_setup_card` + `review.image_gate_picker_card` + `review.locked_takeover_card`）。每个 button 的 `value` 必须含 `action` + `article_id` + 文档里规定的 `payload` 子字段；按钮顺序、label、dangerous 标记不能省。`notify.*` 只能渲染成播报，不得伪装成审核卡，特别是 `notify.hotspots_digest` 不是 Gate A。
3. **发卡**：用 `@larksuite/openclaw-lark` 已经导出的 `sendCardFeishu` / `updateCardFeishu`（见 plugin `index.ts`）。不要再手写 Lark Open API 调用。
4. **按钮回调**：openclaw-lark 已经通过 `dispatchFeishuPluginInteractiveHandler`（`src/channel/interactive-dispatch.ts`）把按钮事件路由到 plugin handler。注册一个 handler，从 `value.action` 取意图、加 `lark_` 前缀就是 AgentFlow 命令名（`approve_b` → `lark_gate_b_approve`），把 `value.article_id` + `event.operator.open_id` + `value.payload` 拼成 command POST 到 `/api/commands`，带 `Authorization: Bearer $AGENTFLOW_AGENT_BRIDGE_TOKEN`。dangerous 命令需要 AgentFlow 端 `AGENTFLOW_AGENT_BRIDGE_ENABLE_DANGEROUS=true` 才会真跑。
5. **@bot 自由文本**：每条 `@bot` 文本必须 POST `{command:"lark_message", params:{text, operator_open_id, operator_name, chat_id, article_id?}}` 到 `/api/commands`，把返回的 `data.reply_card` **原样**渲到群。**禁止 LLM 自己编回复** — 静默 = 契约违反。

### 权威参考（必读 1 次）

- **完整 pseudo-code skeleton**：`docs/openclaw_plugin_integration.md` §"OpenClaw event listener" + §"OpenClaw plugin 注册示例"（含 `/agentflow/events` 路由、按钮 → bridge 转发、auth header 校验）
- **卡片渲染契约**：`backend/agentflow/agent_review/templates/lark_review_cards.md`（Gate A/B/C/D + setup + locked，每张卡的必需字段 / 按钮 / dangerous / payload alias）
- **事件类型表**：`docs/openclaw_plugin_integration.md` §"OpenClaw 该监听的事件类型"（13 种 event_type → 应做什么动作的映射）
- **plugin 源码**：https://github.com/larksuite/openclaw-lark — `index.ts` 看导出的 tool 名，`src/channel/interactive-dispatch.ts` 看按钮分派
- **可直接 copy 的 listener 实现**：`docs/integrations/examples/openclaw_lark_event_listener.ts`（约 350 行 TypeScript，含 7 个 `review.*_card` renderer + `/agentflow/events` 路由 + `forwardButtonToAgentFlow` + `forwardAtBotMessageToAgentFlow` + action 短名 → `lark_*` 命令名映射表）
- **命令字典**：33 个 `lark_*` 命令 + 各自 scope / params / dangerous 标记，全部在 `docs/openclaw_plugin_integration.md` §"命令字典"

### 自检（实现完后必跑）

```
# 在 OpenClaw 端 mock 一次 envelope，看是否成功渲卡到群
curl -X POST http://localhost:<openclaw-port>/agentflow/events \
  -H "Authorization: $AGENTFLOW_AGENT_EVENT_AUTH_HEADER" \
  -H "Content-Type: application/json" \
  -d '{"schema_version":1,"event_type":"review.gate_a_card","article_id":"hs_test_001","payload":{...}}'
# 期望：群里看到带按钮的 Gate A 卡片
# 反例：群里看到纯文本 / 看到 401 / 看到 422 都说明配错了
```

如果短期 plugin handler 还没补完、BEE 暂时只能转发文本到群，**必须明确告诉 user**："飞书交互卡片需要 openclaw-lark plugin 实现 event listener，尚未完成；当前只是 fallback 播报，要走完整 Gate 请用 TG `@CSPostContentAuditBot` 审核。" 不要假装一切正常。

## Anti-patterns (NEVER do these)

1. ❌ **`python -m agentflow.cli.commands xxx`** — double-import 会丢命令注册，用 `blogflow` 入口脚本（pyproject.toml::project.scripts.blogflow = "agentflow.cli.commands:cli"）
2. ❌ **直接 `.env` 凭据 sed/echo** — 绕过 `blogflow onboard` 交互验证 + per-section probe；`blogflow doctor` 第 13 项 stale 报错却 silent fail（因为 daemon 没起来）
3. ❌ **手 `ln -s` 装 skill** — 用 `blogflow skill-install --cursor / --copy / --force`；它会自动 mkdir + skip 已装 + force replace
4. ❌ **手编 `~/.agentflow/topic_profiles.yaml`** — 用 `blogflow topic-profile {init -i, update --from-file, suggest, derive}`；这些命令会 audit + materialize_user_topic_profiles + memory_event 写入
5. ❌ **Framework 代码 hard-code 品牌词**（chainstream / web3 / crypto / kafka / 具体产品名）— framework 是 brand-neutral，brand 内容只从 `publisher_account` 块（per-profile yaml）读
6. ❌ **`force=True` 从 STATE_PUBLISHED rewind** — `triggers.post_publish_ready` 已加 guard；任何 force-rewind 都要 explicit decision + audit
7. ❌ **Callback handler 内 sync LLM 调用** — TG / Lark 都要快速 ack；任何 LLM 调必须 spawn subprocess + 立即回卡
8. ❌ **TG 含未 escape MarkdownV2 chars 不传 `parse_mode=None`** — `_ * [ ] ( ) ~ ` > # + - = | { } . !` 全是 reserved；要么调 `_render.escape_md2(...)`，要么 `parse_mode=None`
9. ❌ **编辑 follow-up 不做一次性消费** — TG `pending_edits.take()` / Lark `lark_pending_edit_consumed` 都必须防止同一槽位被重复消息复用
10. ❌ **`_write_heartbeat` 包外 try-except 之外** — 心跳 best-effort，磁盘满 / 权限错不能让 poll loop 崩；任何 IO 失败都 swallow
11. ❌ **`AGENTFLOW_AGENT_EVENT_WEBHOOK_URL` 指向 AgentFlow 自己的 `/api/commands`** — 这是端口配错。该 URL 只能指向 OpenClaw 侧自己实现的 event listener（如 `/agentflow/events`）。前者只认 command 格式，envelope 格式必返 422，导致 BEE 退化成纯文本推群、按钮全丢。详见上文"Lark Card Rendering"段。
12. ❌ **openclaw-lark 已装但 BEE 仍用纯文本播报扫描结果** — 看到 `openclaw-lark` 在 plugin 列表，必须按 `lark_review_cards.md` 渲 interactive card；纯文本是契约违反，会让 user 失去所有 Gate 按钮。BEE 在 listener 没接好之前**禁止默认开 fallback 模式**，必须先告知 user "卡片渲染未实现，请走 TG"。
13. ❌ **信任 LLM 输出无人审 fact-check** — D2 fill 在 product_facts 之外可能编造：历史事件 / 数据点 / 时间戳 / 产品发布 / 漏洞日期 / 引用人物。skill 应主动提示 user "section X 引用了 'Q1 DuckDB 漏洞'，product_facts 没声明，请核实事实或加 source URL"。任何**未 grounded** 的具体数字 / 日期 / 公司名都是潜在 hallucination。

**Detection**：跑 `blogflow doctor` 13 项 probe；grep `_log.warning` / `_audit kind=spawn_failure`；看 `~/.agentflow/review/audit.jsonl` 末 50 条；任何 silent failure 都应主动 surface。新增：D2 输出后 grep 每节 `compliance_score < 0.85` 必 flag；taboo_violations 非空必处理；具体数字/日期/公司名不在 product_facts 必人审。

## Hotspot Quality Filter

`blogflow write <hid>` 之前必看 hotspot 来源：

- **D1 真热点**：`~/.agentflow/hotspots/YYYY-MM-DD.json` (cron 自动产)，每条含 `source_references` (Twitter / RSS / HN URL list)
- **手工 manual**：`~/.agentflow/hotspots/zzzz_*_manual.json` 或 `search_*_<ts>.json` (`blogflow article-hotspots --filter` / 手编)，可能 `source_references=[]`

| Hotspot 类型 | source_references | 信号 |
|---|---|---|
| D1 cron 产 | 非空 (Twitter URL / RSS / HN) | ✅ 真有市场关注 |
| `--filter` 抓 | 部分非空 | ⚠ 主题对但热度未必 |
| `zzzz_*_manual` | 通常空 | ⚠ 用户自起话题，不算"热点" |

**建议**：写之前 grep `source_references` length > 0；若空，提醒 user "这是 manual hotspot 不是 D1 真热点；要继续吗？"。manual 也可写，但要明白生成的是**主题策划文**而非**热点跟进文**。

## Init Wizard Mode (AI orchestration, 不接收 key)

默认先按首次部署处理；当 user 提"init" / "setup" / "首次" / "怎么开始" / "where do I start"，或未明确说明 runtime 已 ready 时，AI 应：

### 路径 1：Mock 模式（推荐新手 / 演示）

直接给 user 一条命令：

```bash
.venv/bin/blogflow bootstrap --mock --first-run --start-daemon
```

解释：自动 cp .env / 设 MOCK_LLM / 提示 TG creds / 装 skill / 启 daemon / 触 hotspots。**user 在终端 prompt 时输入凭据**（不在 chat 内）。

### 路径 2：Real-key 模式（迭代式）

反复跑：

```bash
.venv/bin/blogflow bootstrap --next-step --json
```

解析 JSON 输出，把 `next_command` 字段给 user 跑。直到 `current_state == "ready"`。

每条 `next_command` 由 user 在**自己终端**执行；当 `blogflow onboard --section X` 时，user 在交互 prompt 里输入凭据。

### 🛡 安全原则（所有 AI 务必遵守）

- **AI 永远不接收凭据 / API key**：当 user 想给 key 时，回："请在终端跑 `blogflow onboard --section <X>`，在终端输入 key 即可（hide_input 已开）。不要复制 key 到 chat。"
- **AI 不写 .env**：所有写入只通过 `blogflow onboard` / `blogflow bootstrap` / `blogflow topic-profile *` 等 framework 命令；这些命令有交互验证 + per-section probe。
- **AI 不破坏框架自带流程**：参见 "Required Init Flows" 段；任何 SET/GET .env / `~/.agentflow/*` 用对应 `blogflow` 命令。

### 典型对话

```
user: "我刚装 agentflow, 怎么开始？"
AI: "你想 mock 还是 real-key？"
   - mock: "跑 `.venv/bin/blogflow bootstrap --mock --first-run --start-daemon`，
            user 看终端 prompt 输入 TG token + chat_id 即可。"
   - real: "跑 `blogflow bootstrap --next-step --json` 我看下你现在缺啥。"

user: <跑完, 贴 JSON 输出>
AI:   <解析 next_command>
      "下一步跑：`blogflow onboard --section telegram`，在终端 prompt 里贴 token。"

user: "做完了，下一步呢？"
AI:   "再跑 `blogflow bootstrap --next-step --json`"
   ...直到 ready
```

## After D2: Review the Draft

`blogflow write <hid> --auto-pick` 跑完后, draft 在 `~/.agentflow/drafts/<aid>/draft.md`。**不要假设 LLM 输出可直接 publish**——必看 compliance + 内容 fact-check：

### Compliance / Taboo 检查

读 `metadata.json` 的每节 `compliance_score` + `taboo_violations`：

| 现象 | 推荐动作 |
|---|---|
| 节 compliance ≥ 0.85, 0 violations | ✅ Gate B approve |
| 节 compliance 0.5-0.85, 1-2 violations (如段长 > 125 字) | ✏️ `blogflow edit <aid> --section <N> --command "缩短至 100 字以内 + 拆段"` |
| 节 compliance < 0.5 (taboo_pattern hit / 多段超长) | 🔁 rewrite once：`B:rewrite` (`blogflow fill` 同 indices) |
| ≥ 2 节 compliance < 0.5 | 🚫 reject + 重选 hotspot |
| `taboo_pattern: '首先...其次...最后'` 命中 | ✏️ edit, 改写为陈述句不用 enumeration 套话 |

### 内容 fact-check (LLM 真有可能编)

**LLM 在 product_facts 之外可能编造历史事件、数据、时间戳、产品发布等**。skill 应提醒 user 主动检查：

- "数百万笔交易" / "Q1 遭遇 X 漏洞" 等量化 / 时间事件 → 必须人核
- 引用的项目 / 公司 / 人物 → 抽 1-2 个去 grep `~/.agentflow/style_corpus/` 或 google 一次
- 文章末尾若有"加入我们 / 联系我们"销售话术 → ✏️ edit 删除（content_tone 反模式）

### Decision Matrix (recap)

```
clean (≥0.85 + 0 violations)         → B:approve → image-gate
1 节有问题 (1-2 violations)            → B:edit 该节；Lark 可用输入框 comment 或 @bot → lark_apply_pending_edit
1 节崩坏 (compliance <0.5)             → B:rewrite / lark_refill (dangerous opt-in, max 2 rewrite)
多节崩坏                              → B:reject 或 escalate to L:* (round 3+ → drafting_locked_human)
LLM 编造关键事实                        → ✏️ edit + 加 source URL 或删除虚构段
```

## Profile Health Check

`blogflow topic-profile show --profile <id>` 第一步先看 profile 完整度。**不完整的 profile 会让 D2 输出空洞 / 跑题**。

完整 profile 必须含：
- `label` (display name)
- `publisher_account.brand` (与 voice 一致的品牌名)
- `publisher_account.voice` ∈ {first_party_brand / observer / personal}
- `publisher_account.do` (≥ 2 条 voice 规则)
- `publisher_account.dont` (≥ 2 条 voice 反模式)
- `publisher_account.product_facts` (≥ 3 条；framework 内核命题)
- `publisher_account.default_tags` (≥ 3 个；Medium / Substack 标签 fallback)
- `keyword_groups` (≥ 3 组；D1 hotspot 命中用)

**任一缺则**：跑 `blogflow topic-profile init -i --profile <id>` 交互补；或 `blogflow topic-profile derive --profile <id>` 让 LLM 从 default_description 反推 + 走 suggestion 流。

`blogflow bootstrap --next-step --json` 含 `missing_profile` 状态时，AI 不要直接进 D2，先补 profile。

