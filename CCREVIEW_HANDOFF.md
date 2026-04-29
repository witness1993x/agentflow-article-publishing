# CCreview Handoff

## 当前结论

项目形态已经从早期的 **Next.js + FastAPI Web 应用** 收敛为 **skill-first + `af` CLI**：

1. 日常入口是 Claude Code 里的 5 个 skill（`.claude/skills/`）
2. 所有实际能力由 `af` CLI 承载（`backend/agentflow/cli/commands.py`）
3. 旧的 `api/` + `frontend/` 已整体归档到 `_legacy/`，不再维护

这意味着本轮 CCreview 的重点，**不是** 早期 Wave 2F / 2G / 3 的 HTTP 路由 + Next.js 页面，而是：

- 5 个 skill 的 prompt 契约是否和 `af` CLI 行为匹配
- `af <subcommand> --json` 输出 schema 是否稳定
- 默认自动成稿（`af write --auto-pick`）是不是真的在 mock 和实 key 都可靠
- 记忆层（`events.jsonl`）边界是否清晰

## 这次交付覆盖范围

### 已完成的主功能

- `af` CLI 全部 11 个子命令已实现（`learn-style / hotspots / hotspot-show / write / fill / edit / image-resolve / draft-show / preview / publish / memory-tail / run-once`）
- 5 个 skill 文件已就位：`agentflow / agentflow-style / agentflow-hotspots / agentflow-write / agentflow-publish`
- 默认自动成稿（`--auto-pick` → skeleton + fill `0/0/0`）已是 D2 主路径
- 统一记忆事件流落地到 `~/.agentflow/memory/events.jsonl`
- D3 平台适配、D4 多平台发布、发布历史、retry、style console 均已完整
- `MOCK_LLM=true` 下整条链路可跑通（2026-04-24 已回归）

### 当前默认产品路径

1. 用户在 Claude Code 里调 `/agentflow-hotspots`
2. skill 调 `af hotspots --json` 拉今日热点 + angles
3. 用户挑 `hotspot_id + angle`，调 `/agentflow-write <hotspot_id>`
4. skill 调 `af write <hid> --auto-pick --json`，后端生成 skeleton 后直接 `0/0/0` fill 完整 draft
5. 用户进入 skill 的交互式编辑循环（`af edit --section N --command "..."`）
6. 完成后调 `/agentflow-publish <article_id>`，skill 串 `af preview + af publish`
7. 关键行为自动落到 `~/.agentflow/memory/events.jsonl`

## 关键改动文件

### 自动成稿主链路（D2）

- `backend/agentflow/cli/commands.py`（`write` 子命令 + `--auto-pick`）
- `backend/agentflow/agent_d2/main.py`
- `backend/prompts/d2_skeleton_generation.md`
- `backend/prompts/d2_paragraph_filling.md`
- `backend/prompts/d2_interactive_edit.md`
- `.claude/skills/agentflow-write/SKILL.md`

关键变化：

- `af write <hid>` 默认要求手动 `--title/--opening/--closing`
- `af write <hid> --auto-pick` 一次性生成 skeleton + `0/0/0` 选择并写 `draft_ready`
- skill prompt 里把 `--auto-pick` 作为默认推荐路径

### 统一记忆层

- `backend/agentflow/shared/memory.py`（事件 schema + append 写入）
- `backend/agentflow/cli/commands.py`（各命令结束后写事件）

事件流：

- 路径：`~/.agentflow/memory/events.jsonl`
- schema：`schema_version / ts / event_type / article_id / hotspot_id / payload`

当前已接入的事件类型：

- `article_created`（write）
- `fill_choices`（write / fill）
- `section_edit`（edit）
- `hotspot_review`（hotspots）
- `preview`（preview）
- `publish`（publish）
- `learn_style`（learn-style）
- `image_resolved`（image-resolve）

### skill 编排层（UX）

- `.claude/skills/agentflow/SKILL.md`（入口总览）
- `.claude/skills/agentflow-style/SKILL.md`
- `.claude/skills/agentflow-hotspots/SKILL.md`
- `.claude/skills/agentflow-write/SKILL.md`
- `.claude/skills/agentflow-publish/SKILL.md`

当前 skill 语义：

- `agentflow-hotspots`：强调“选一个 hotspot + angle 就交给 write”
- `agentflow-write`：强调“默认自动成稿，后续只做局部编辑”
- `agentflow-publish`：保留 preview / 图片 resolve / 发布，并将这些行为纳入记忆事件流

## Reviewer 最应该看的点

### 1. skill prompt ↔ CLI 契约是否一致

重点看：

- skill 文本里调的 `af` 命令行参数、输出字段，是否完全匹配 `commands.py` 的实际实现
- skill 对错误场景（409 未处理图片、404 article_id 不存在、400 参数不合法）的期望是否和 CLI 实际返回一致
- `--json` 模式下 stdout 是纯 JSON，log 全走 stderr（已验证）

### 2. 默认自动成稿是否真的稳定

重点看：

- `af write <hid> --auto-pick --json` 是否总能拿到完整 draft
- mock 模式下是否稳定；实 key 模式下是否有模型输出 JSON 解析失败的兜底
- 刷新 / 重入 / 用旧 `article_id` 二次 `write` 时行为是否幂等

### 3. 记忆层是否足够薄、足够稳

重点看：

- `events.jsonl` 保持 append-only，不污染单篇 `metadata.json`
- 事件 payload 足够小、语义稳定
- 各 CLI 命令写 memory event 的时机合理，无漏记/重复记

### 4. 单篇状态与跨篇状态边界

重点看：

- `draft.md`、`metadata.json`、`platform_versions/*.md`、`events.jsonl`、`publish_history.jsonl` 职责是否清晰
- `preview_ready`、`draft_ready`、`published` 状态流转是否会被重复覆盖
- 图片上传、局部编辑、重新 fill、发布重试之间是否有状态竞争

### 5. v0.1 范围控制是否仍然合理

重点看：

- `run-once` 仍是“最小编排”，不是全自动 orchestration
- 记忆层目前只是事件沉淀，不包含“基于记忆自动改默认策略”的闭环
- 真实 API key 路径正在推进 smoke，当前默认验收仍是 mock

## 已完成验证

### mock 端到端（2026-04-24 实跑）

```bash
cd backend && source .venv/bin/activate

MOCK_LLM=true PYTHONPATH=. af hotspots --json 2>/dev/null > /tmp/out.json
# → 5 hotspots，3 个 collector 都走 mock ✓

HID=$(python -c 'import json; print(json.load(open("/tmp/out.json"))["hotspots"][0]["id"])')
MOCK_LLM=true PYTHONPATH=. af write "$HID" --auto-pick --json 2>/dev/null > /tmp/art.json
# → skeleton + draft 齐全 ✓

AID=$(python -c 'import json; print(json.load(open("/tmp/art.json"))["article_id"])')
MOCK_LLM=true PYTHONPATH=. af preview "$AID" --json 2>/dev/null >/dev/null
# → ghost_wordpress.md + linkedin_article.md 生成 ✓

MOCK_LLM=true PYTHONPATH=. af publish "$AID" --force-strip-images --json 2>/dev/null
# → 两个平台 status=success ✓

MOCK_LLM=true PYTHONPATH=. af memory-tail --limit 6 --json
# → article_created / fill_choices / preview / publish 都在 ✓
```

产物齐全：`~/.agentflow/drafts/<aid>/{skeleton.json, draft.md, metadata.json, d3_output.json, platform_versions/*.md}`。

### 已知非阻塞项

- Python 3.14 bundled venv 在某些平台可能需要 fallback 到 3.11/3.12
- `run-once` 依然是半自动交接（跑完 D1 就停），不是全自动阻塞式串行编排
- 当前以 mock 模式验收为主，真实平台凭证 smoke 正在做
- 记忆层当前只做事件沉淀，还没有消费层来自动调优默认策略
- 旧 `_legacy/tests/test_p1_api.py` 不再跟随主路径跑；新 CLI 层暂无独立单测（skill-first 以手工 smoke 为准）

## 推荐复现方式

### 环境准备

```bash
cd backend
source .venv/bin/activate
# 如果还没装：
# python3 -m venv .venv && source .venv/bin/activate
# pip install -r requirements.txt
# cp .env.template .env  # MOCK_LLM=true
```

### mock 主流程（参见上面已完成验证部分）

### skill-first 复现（推荐）

在项目根目录打开 Claude Code，依次调：

1. `/agentflow-hotspots`（拿 hotspot_id + angle）
2. `/agentflow-write <hotspot_id>`（交互式写作）
3. `/agentflow-publish <article_id>`（预览 + 发布）

## 如果 CCreview 提出问题

建议带回这些信息：

- 具体 `af` 命令行 + `--json` 的请求 / 响应体
- 出问题文章对应的 `~/.agentflow/drafts/<aid>/metadata.json`
- 对应的 `draft.md` 和 `platform_versions/*.md`
- 相关 memory event 片段（`af memory-tail --limit 20 --json`）
- `~/.agentflow/logs/agentflow.log` 的 tail
- `~/.agentflow/logs/llm_calls.jsonl` 对应行（含 `mocked=bool`）
- 是发生在 mock 还是实 key 模式
