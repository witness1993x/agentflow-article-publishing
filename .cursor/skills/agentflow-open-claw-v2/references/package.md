# agentflow-open-claw v2.7

AgentFlow 文章发布框架（agentflow-article-publishing）的 Cursor / Claude Code / OpenClaw 兼容 skill 包。

## 标准结构

| 路径 | 用途 |
|---|---|
| `SKILL.md` | 触发后加载；包含硬规则、CLI-first 工作流和按需阅读入口 |
| `references/` | 长参考文档、示例和复杂任务模板 |
| `assets/` | YAML 模板；作为 `af topic-profile ... --from-file` 的参数输入 |

包内不包含 `backend/agentflow/` 源码，也不包含 runtime 依赖。这样可以避免 harness 在执行偏离预期时修改实现代码，并降低安装负担。

## 这是 skill，不是 runtime

要真跑 AgentFlow，必须配套 runtime 包，例如 `agentflow-framework-{YYYYMMDD}-slim.zip`。skill 仅给 LLM 决策上下文。

真实运行还需要：

1. runtime repo：含 `backend/agentflow/` 和 `pyproject.toml`
2. Python venv：`cd backend && python3 -m venv .venv && pip install -e .`
3. `.env`：mock-only 至少需要 Telegram 相关配置和 `MOCK_LLM=true`
4. `~/.agentflow/`：首次通过 `af review-init` / `af bootstrap` 创建

OpenClaw/Cursor/Claude Code 是 harness，不需要为 skill 单独启动守护进程。`af review-daemon` 是 Telegram review 业务运行面，只有在实际跑 bot 时才需要。

## 版本历史

- v2.7：把默认入口改为首次部署 / 初始化续跑；description 与正文 prompt 都先要求检查 runtime repo、venv、`.env`、`~/.agentflow/`。
- v2.6：压缩 `SKILL.md` frontmatter description，保持 Cursor/OpenClaw 元数据轻量；确认标准包结构可直接分发。
- v2.5：整理为标准 skill 包结构；新增 `references/` 与 `assets/`；明确 CLI-only、no-source、no-extra-daemon。
- v2.2：加入 Required Init Flows 与 Anti-patterns，防止云端跳过 framework 命令直接改 runtime 配置。
- v2.1：加入 What this skill is NOT 与 Required Runtime。
- v2：修正 14 个 STATE，强调 brand-neutral 与 progressive disclosure。
- v1：已废弃；含陈旧 5-state 模型。
