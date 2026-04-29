# Hotspot To GitHub Pipeline

一个用于把行业热点转成 GitHub 项目决策与执行工作区的实验性流水线。

这套流程回答的不是“要不要追热点”这么简单，而是：

1. 这个热点是否真实、仍在窗口内？
2. 它更适合被做成哪种 GitHub 项目？
3. 应该 `fork`、`template clone`，还是 `new repo`？
4. 这个项目是否能被构造成可编译、可测试的仓库？
5. 最终是否值得真的发布到 GitHub？

当前版本还支持两件自动化动作：

- 自动从 GitHub 抓取 candidate repos
- 对 candidate repos 做可解释打分与排序
- 执行后自动把结果回写到 gate、probe-run、memo、review 和 pool

## 与 Window Gate 的关系

这个目录复用了 `experimental/window-gate-framework` 的工作方式：

- `README + templates + examples + cases + pool + scaffold`
- 结构化 YAML
- memo / probe / review 三件套
- 统一 ID 和 case 目录

但这里不直接复用原来的需求窗口语义，而是换成更贴近 GitHub 落地的 pipeline 语义。

如果你想看完整方案，先读 `FRAMEWORK_SPEC.md`；如果你想看它如何从 Window Gate 迁移而来，再读 `WINDOW_GATE_ALIGNMENT.md`。

## 核心结构

这套框架分成两层：

- `Layer A: Framing`，先把热点问题化
- `Layer B: Gates`，再判断 repo 路由、buildability 和发布价值

## Layer A: Framing

### Gate 0: Reframe

先回答这个热点是否值得被表达为一个 GitHub 项目，而不是一篇内容、一条 tweet 或一次纯调研。

关键问题：

- 热点对应的真实问题是什么？
- 为什么 GitHub 项目是合适承接方式？
- 这个项目更像 demo、starter、SDK、CLI、agent workflow 还是 MCP server？
- 什么证据会推翻“值得做仓库”的判断？

## Layer B: Gates

### Gate 1: Hotspot Signal

判断热点是否真实、是否仍处在值得跟进的窗口。

关键问题：

- 热点是否有持续信号，而不是单次噪音？
- 是否有行为证据、数据证据或真实项目出现？
- 这个话题是否已有足够多的技术活动和用户讨论？
- 现在进入是否还来得及？

### Gate 2: Project Shape

判断热点应该被做成什么样的 GitHub 项目。

关键问题：

- 最适合的项目形态是什么？
- 受众是谁？
- 最小可交付仓库长什么样？
- 是偏模板、工具、示例还是生产级项目？

### Gate 3: Repo Routing

判断仓库承接策略。

关键问题：

- `fork_existing`
- `template_clone`
- `new_repo`

还要判断：

- 是否有合适上游仓库
- 是否存在 license 风险
- 直接 fork 会不会让价值过于稀释
- 是否更适合从现有模板起盘

### Gate 4: Buildability

判断项目是否能被构造成“可编译、可测试、可解释”的仓库。

关键问题：

- 是否有明确的 build 命令
- 是否能在本地安装依赖
- 是否能跑最小测试或 smoke check
- 是否需要私有资源或账号才能跑通
- 文档是否足以支撑复现

### Gate 5: Publish Decision

判断是否值得真的推到 GitHub。

关键问题：

- 这个仓库是否有公开价值
- 命名、README、示例、测试是否到最低可发布标准
- 是立即发布、先本地 probe，还是直接 drop

## 决策输出

每次 pipeline run 最终只落到四种状态之一：

- `watch`: 热点值得继续观察，但还不进入 repo 动作
- `probe`: 值得做一次本地或临时仓库实验，验证 repo 路由和 buildability
- `publish`: 已经值得正式发布到 GitHub
- `drop`: 不值得继续投入

## 命名规范

- `Hotspot ID`: `HSP-001`, `HSP-002`, `HSP-003`
- `Case folder`: `HSP-001-YYYY-MM-DD-slug`

建议规则：

- 一个热点/项目判断只使用一个主 `HSP-xxx`
- 所有模板都写同一个 `Hotspot ID`
- `pipeline-pool.md` 是总索引
- `cases/` 里保留具体工作副本

## 最小工作流

1. `intake`：记录热点和初始判断
2. `gate round`：完成一次结构化 pipeline 判断
3. `memo`：输出本轮发布决策
4. `build probe`：必要时做本地构建验证
5. `review`：复查状态变化
6. `pool`：在总索引里管理所有 case

## 脚手架

如果你想快速为一个新热点生成完整工作目录，可以直接运行：

```bash
python experimental/hotspot-to-github-pipeline/scaffold_pipeline.py --hotspot-name "Your Hotspot"
```

常用参数：

```bash
python experimental/hotspot-to-github-pipeline/scaffold_pipeline.py \
  --hotspot-name "AI MCP Server" \
  --owner "founder" \
  --status "watch" \
  --repo-strategy "new_repo" \
  --project-shape "mcp_server" \
  --thesis "先验证项目形态和 buildability，再决定是否发布"
```

脚手架会自动生成：

- 统一的 `HSP-xxx` 编号
- 统一的 case 目录名
- `01-hotspot-intake.md`
- `02-pipeline-gate.yaml`
- `03-publish-decision-memo.md`
- `04-build-probe-run.md`
- `05-review-checkpoint.md`
- 同时自动向 `pipeline-pool.md` 追加一条索引

常用参数：

- `--status`: `draft / watch / probe / publish / drop`
- `--repo-strategy`: `undecided / fork_existing / template_clone / new_repo`
- `--project-shape`: `undecided / demo / starter / sdk / cli / agent_workflow / mcp_server`
- `--thesis`: 预填一条简短判断
- `--next-review-date`: 手动指定复查日期
- `--review-days`: 不手动指定时，自动用多少天后作为复查日期
- `--pool-file`: 指定 pipeline pool 文件
- `--skip-pool`: 只生成 case，不写入 pool

## 文件说明

- `FRAMEWORK_SPEC.md`: 完整框架方案，说明这套 pipeline 如何运行
- `WINDOW_GATE_ALIGNMENT.md`: 与 `window-gate-framework` 的迁移关系和字段级对照
- `pipeline-pool.md`: 真正使用中的 pipeline 总索引
- `templates/hotspot-intake.template.md`: 热点入口
- `templates/pipeline-gate.template.yaml`: 一轮结构化判断
- `templates/publish-decision-memo.template.md`: 发布决策输出
- `templates/build-probe-run.template.md`: 编译测试探针记录
- `templates/review-checkpoint.template.md`: 周期复查
- `templates/pipeline-pool.template.md`: pipeline 索引模板
- `scaffold_pipeline.py`: 生成新 pipeline case 的零依赖脚手架
- `run_pipeline.py`: 读取 gate 文件并执行 inspect / probe / publish
- `cases/`: 每个热点的工作副本
- `workspaces/`: 执行层产生的本地工作区
- `examples/*.md|*.yaml`: 示例填法

## 执行层

当前版本已经补上执行层，入口是：

```bash
python experimental/hotspot-to-github-pipeline/run_pipeline.py --case-dir "<case-dir>" --mode inspect
```

支持四种模式：

- `inspect`: 只读取 `02-pipeline-gate.yaml` 并输出计划
- `discover`: 自动用 GitHub 搜索 candidate repos，并可写回 `candidate_repos`
- `probe`: 按 `repo_strategy` 准备本地 workspace，并执行 install/build/test
- `publish`: 在 `probe` 基础上尝试真正发布到 GitHub

安全默认值：

- 不加 `--execute` 时，只做 dry-run
- `publish` 模式必须同时传 `--execute --allow-publish`
- 不加 `--execute` 时，不会回写 gate / probe-run / pool

自动抓取 candidate repos：

```bash
python experimental/hotspot-to-github-pipeline/run_pipeline.py \
  --case-dir "experimental/hotspot-to-github-pipeline/cases/HSP-001-2026-04-27-ai-mcp-server" \
  --mode discover
```

真正写回 candidate repos 和推荐策略：

```bash
python experimental/hotspot-to-github-pipeline/run_pipeline.py \
  --case-dir "experimental/hotspot-to-github-pipeline/cases/HSP-001-2026-04-27-ai-mcp-server" \
  --mode discover \
  --execute
```

示例：

```bash
python experimental/hotspot-to-github-pipeline/run_pipeline.py \
  --case-dir "experimental/hotspot-to-github-pipeline/cases/HSP-001-2026-04-27-ai-mcp-server" \
  --mode probe
```

真正执行本地 probe：

```bash
python experimental/hotspot-to-github-pipeline/run_pipeline.py \
  --case-dir "experimental/hotspot-to-github-pipeline/cases/HSP-001-2026-04-27-ai-mcp-server" \
  --mode probe \
  --execute
```

真正执行 publish：

```bash
python experimental/hotspot-to-github-pipeline/run_pipeline.py \
  --case-dir "experimental/hotspot-to-github-pipeline/cases/HSP-001-2026-04-27-ai-mcp-server" \
  --mode publish \
  --execute \
  --allow-publish
```

执行后自动回写：

- `02-pipeline-gate.yaml`
- `04-build-probe-run.md`
- `03-publish-decision-memo.md`
- `05-review-checkpoint.md`
- `pipeline-pool.md`

新增字段重点包括：

- `gate_3_repo_routing.discovered_query`
- `gate_3_repo_routing.recommended_strategy`
- `gate_3_repo_routing.recommended_reason`
- `candidate_repos[*].score`
- `candidate_repos[*].ranking_reason`
- `execution_state.discovery`
- `execution_state.probe`
- `execution_state.publish`

## 目录结构

```text
experimental/hotspot-to-github-pipeline/
├── README.md
├── FRAMEWORK_SPEC.md
├── WINDOW_GATE_ALIGNMENT.md
├── pipeline-pool.md
├── scaffold_pipeline.py
├── run_pipeline.py
├── cases/
│   └── README.md
├── workspaces/
│   └── README.md
├── templates/
│   ├── hotspot-intake.template.md
│   ├── pipeline-gate.template.yaml
│   ├── publish-decision-memo.template.md
│   ├── build-probe-run.template.md
│   ├── review-checkpoint.template.md
│   └── pipeline-pool.template.md
└── examples/
    ├── agent-observability-dashboard.watch.example.md
    ├── ai-mcp-server.probe.example.md
    ├── ai-mcp-server.pipeline-gate.example.yaml
    ├── browser-agent-framework.drop.example.md
    └── vertical-sdk.publish.example.md
```

## 下一阶段扩展点

当前版本已经能读取 gate、准备 workspace、执行 build/test，并在满足条件时调用 `gh`。

下一阶段可以继续补：

- 基于探针结果自动调整 `decision.final_status`
- 参考 `docs/integrations/AGENT_BRIDGE.md` 作为外部编排边界

## 环境说明

当前这套实验内容以文档、模板和脚手架为主，不依赖 `.env`。

如果下一阶段需要真实调用 GitHub API、`gh` CLI 或远程执行，再在本目录下补 `.env.template` 更合适。
