# agentflow-open-claw v2.2

AgentFlow 文章发布框架（agentflow-article-publishing）的 Cursor / Claude Code / OpenClaw 兼容 skill 包。

## 文件清单

| 文件 | 大小 | 加载时机 |
|---|---|---|
| `SKILL.md` | ~2.5 KB | description 匹配触发后加载（Tier 2） |
| `reference.md` | 9.1 KB | 提及 state machine / runtime artifact / api key 时按需读 |
| `examples.md` | 3.9 KB | 复杂任务复盘时参考 |
| `template.md` | 1.5 KB | 复杂任务可选 5-段仪式 |

总 ~17 KB；progressive disclosure：trigger 时仅 SKILL.md 加载（约 450 token）。

## ⚠ 这是 skill, 不是 runtime

要**真跑** AgentFlow，必须**配套 runtime 包** (`agentflow-framework-{YYYYMMDD}-slim.zip`)。skill 仅给 LLM 决策上下文。详见 SKILL.md 末尾的 "What this skill is NOT" + "Required Runtime"。

## v2.2 新增（vs v2.1）

- **Required Init Flows** 段（10 条 MUST/NEVER 规则）：framework 自带命令必走，绕过会 silent fail
- **Anti-patterns** 段（10 条 ❌）：常见踩坑明示，含 detection 路径
- description 加 5 行 INIT obligation（高权重 trigger 后规则）
- 主因：v2.1 在云端 OpenClaw 测试时仍发生跳过 init 直接 sed .env 行为；v2.2 把这些路径**主动强制**而不是 SKIP 被动警告

## 版本历史

- **v2.2** (current, 2026-04-28): 加 "Required Init Flows" + "Anti-patterns" 高权重段；description 加 INIT obligation；防止云端跳过 framework 自带命令直接改 runtime 配置。
- v2.1: 加 "What this skill is NOT" + "Required Runtime" 段防云端误解 runtime 依赖。
- v2: trigger 14 / SKIP 5；14 STATE_* 准确；brand-neutral；progressive disclosure。
- v1 (deprecated): state 列旧 5 个；test_p1_api 已 legacy；frontend npm；强制 5-段仪式。

## 升级路径

v2.1 → v2.2: in-place 改 SKILL.md，末尾加 2 段 + description 加 5 行；不影响 reference.md / examples.md / template.md。

## 触发约定

- TRIGGER: `open claw` / `agentflow` / `Gate A/B/C/D` / `review-daemon` / `_handle_callback` / `post_gate_*` / `topic_profiles.yaml schema` / `/list` / `publish-mark` / `PR:mark` / `L:critique` / `PD:dispatch` / `I:cover_only` / `STATE_DRAFT_PENDING_REVIEW` / `STATE_CHANNEL_PENDING_REVIEW`
- SKIP: 通用 Python / Click / FastAPI / pytest 通用问题；编辑 user 数据 `topic_profiles.yaml`；一次性 bash / grep / ls；user 显式说"忽略 skill"；改 `.env` 凭据；**没 runtime 还要"运行 agentflow"** (新加)
