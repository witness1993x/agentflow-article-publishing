# Window Gate Alignment

这个文件说明 `hotspot-to-github-pipeline` 如何从 `window-gate-framework` 迁移而来。

## Why A Separate Folder

虽然两者都使用：

- `README`
- `templates`
- `examples`
- `cases`
- `pool`
- `scaffold`

但二者关注的问题不同：

- `window-gate-framework`: 判断机会值不值得做
- `hotspot-to-github-pipeline`: 判断热点是否值得被做成 GitHub 项目，并推进到执行

## Structural Mapping

| Window Gate | Hotspot To GitHub Pipeline |
|---|---|
| Opportunity Intake | Hotspot Intake |
| Gate 1 Signal | Gate 1 Hotspot Signal |
| Gate 2 Demand Window | Gate 2 Project Shape |
| Gate 3 Product Window | Gate 3 Repo Routing + Gate 4 Buildability |
| Gate 4 Action | Gate 5 Publish Decision |
| Probe Run | Build Probe Run |
| Decision Memo | Publish Decision Memo |
| Opportunity Pool | Pipeline Pool |

## Semantic Shift

### From Opportunity To Repository

原框架问：

- 这个机会值不值得做？

新框架问：

- 这个热点值不值得被表达成 GitHub 项目？
- 这个仓库应该长什么样？
- 应该 route 到哪种 repo strategy？

### From Action To Publish

原框架中的 `build` 更接近“开始做产品”。

新框架中的 `publish` 更接近“已经具备公开仓库价值，可以真正发到 GitHub”。

## Shared Patterns

两者共享以下工作方式：

- 用 YAML 作为结构化判断的事实源
- 用 memo 输出本轮结论
- 用 probe 记录关键实验
- 用 review checkpoint 记录状态变化
- 用 pool 管理多个并行 case
- 用脚手架生成标准化工作目录

## New Concepts In Pipeline

新框架相对 `window-gate-framework` 新增了这些概念：

- `project_shape`
- `repo_strategy`
- `candidate_repos`
- `build_commands`
- `repo_plan`
- `publish` 执行层

## Execution Boundary

`window-gate-framework` 到模板和脚手架为止。

`hotspot-to-github-pipeline` 继续往前走了一层，通过 `run_pipeline.py` 进入：

- local workspace
- clone/init repo
- build/test
- optional GitHub publish

所以这两个 experimental 目录的关系可以理解成：

- `window-gate-framework` 是通用决策骨架
- `hotspot-to-github-pipeline` 是其中一个面向 GitHub 项目落地的垂直化实现
# Window Gate Alignment

这份文档用于说明 `hotspot-to-github-pipeline` 与 `window-gate-framework` 的关系，先完成框架级对齐，方便后续继续补字段级映射。

完整运行方案见 `FRAMEWORK_SPEC.md`；本文只负责解释迁移关系和字段级对照。

## 对齐原则

`hotspot-to-github-pipeline` 不是从零新造一套流程，而是把 `window-gate-framework` 的骨架迁移到 “热点 -> GitHub 项目 -> 可发布仓库” 这个语义域里。

优先保留的东西：

- 同样的目录骨架：`README / templates / examples / cases / pool / scaffold`
- 同样的文件节奏：`intake -> gate yaml -> memo -> probe -> review`
- 同样的 case 主键机制：`ID + 日期 + slug`
- 同样的 Gate 驱动工作方式：先 framing，再做结构化判断，再输出 memo 和复查

优先替换的东西：

- 从“机会是否值得下注”切到“热点是否值得转成 GitHub 项目并发布”
- 从“需求/产品/行动窗口”切到“项目形态/仓库路由/buildability/发布决策”
- 从 `build` 状态切到 `publish` 状态

## 框架映射

### 1. 目录与文件骨架

- `opportunity-pool.md` -> `pipeline-pool.md`
- `01-opportunity-intake.md` -> `01-hotspot-intake.md`
- `02-window-gate.yaml` -> `02-pipeline-gate.yaml`
- `03-decision-memo.md` -> `03-publish-decision-memo.md`
- `04-probe-run.md` -> `04-build-probe-run.md`
- `05-review-checkpoint.md` -> `05-review-checkpoint.md`

这意味着流水线版本仍然沿用 window-gate 的“五件套”，只是把第 3、4 个产物改成更靠近 repo 决策与构建验证。

### 2. 主键与命名

- `Opportunity ID` -> `Hotspot ID`
- `OPP-xxx` -> `HSP-xxx`
- `Opportunity name` -> `Hotspot name`
- case 目录命名规则保持不变，只是前缀从 `OPP` 换成 `HSP`

### 3. 状态机

- `draft` 保留
- `watch` 保留
- `probe` 保留
- `build` -> `publish`
- `drop` 保留

这里不是简单改词。`window-gate` 的 `build` 表示“值得进入产品化”，而 pipeline 的 `publish` 表示“值得公开发布到 GitHub”。因此两者在动作强度上相近，但交付对象不同。

## Gate 语义映射

### 1. 可以直接继承的 Gate

- `gate_0_reframe` -> `gate_0_reframe`
- `gate_1_signal` -> `gate_1_hotspot_signal`

这两层的核心逻辑基本没变，只是对象从“机会/需求”变成“热点/技术话题”。

### 2. 被重新解释的中段 Gate

- `gate_2_demand_window` -> `gate_2_project_shape`
- `gate_3_product_window` -> `gate_3_repo_routing`

这不是字段级 1:1 替换，而是把原来“窗口是否形成”的判断，转成“应该被做成什么项目”以及“仓库从哪里起盘”。

也就是说，pipeline 在中段不再只判断窗口强弱，而是更早进入承接形态和路线选择。

### 3. 被拆开的末段 Gate

- `gate_4_action` -> `gate_4_buildability` + `gate_5_publish_decision`

这是本次迁移里最重要的结构变化。

`window-gate` 的 `Action Gate` 同时承担了三个问题：

- 为什么该由我们做
- 是否适合现在投入
- 下一步应该观察、probe 还是直接做

在 pipeline 里，这一层被拆成两段：

- `gate_4_buildability`：仓库能不能被装起来、跑起来、解释清楚
- `gate_5_publish_decision`：即使能跑，是否已经值得公开发布

因此可以把它理解为：原版的“团队是否该下注”，被替换成“仓库是否能落地”加“公开发布是否成立”。

## YAML 结构对齐

### 1. 明确保留的顶层区块

以下顶层区块在两套 YAML 中都保留：

- `meta`
- 问题定义区块
- `gate_0`
- 各评分 Gate
- `decision`
- `notes`
- `review_log`

说明 pipeline 并没有推翻 window-gate 的 YAML 使用方式，只是替换了中间语义。

### 2. 明确重命名的顶层区块

- `decision_question` -> `hotspot_question`
- `probe_plan` -> `build_probe`

这两处已经从“机会判断”改成“热点转仓库判断”。

### 3. pipeline 新增的顶层区块

- `source_context`

这是 window-gate 中没有单独拉出的部分。因为热点判断比一般机会判断更依赖来源、话题脉络和外部链接，所以单独升成一级区块。

### 4. 暂不再作为一级结论的内容

以下内容在 pipeline 中不再保持原版的中心地位：

- `timing_verdict`
- `why_us_hypothesis`
- `gate_2_demand_window.window_assessment`
- `gate_3_product_window.window_assessment`

不是这些问题不重要，而是它们被吸收到更具体的 repo 语义里，例如：

- “现在是不是窗口”被分散到 `hotspot signal`、`project shape`、`publish decision`
- “为什么是我们做”弱化为 repo 路由、可维护性、公开价值，而不再单独作为一个团队下注 Gate

## 其他产物映射

### Intake

`hotspot-intake` 延续了 `opportunity-intake` 的问法和节奏，但把初始视角从：

- 目标用户 / 痛点 / workaround / timing

切成：

- 目标开发者或用户 / 可疑项目机会 / project shape / repo strategy

### Memo

`publish-decision-memo` 延续了原版 memo 的结构，但新增了：

- `Routing Decision`
- `Build / Publish Logic`
- build / test command

同时移除了原版 memo 中更偏“创业下注”的表达，例如 `Why us` 和明确的 `Timing verdict`。

### Probe

`build-probe-run` 继承 `probe-run` 的最小实验思路，但实验对象已经从“验证机会”切到“验证仓库是否能 clone / install / build / test”。

### Review

`review-checkpoint` 继续保留复查节奏，但关注点从 timing / primary constraint 变成 project shape / repo strategy 的变化。

## 脚手架参数映射

保留的参数模式：

- `--owner`
- `--question`
- `--slug`
- `--output-dir`
- `--status`
- `--thesis`
- `--next-review-date`
- `--review-days`
- `--pool-file`
- `--skip-pool`

从 window-gate 迁移后发生的关键变化：

- `--name` -> `--hotspot-name`
- `--timing` 被移除
- 新增 `--project-shape`
- 新增 `--repo-strategy`

这说明 pipeline 脚手架已经完成了“从窗口判断器到 repo 决策器”的入口改造。

## YAML 字段级对照

这一节按 `window-gate.template.yaml` 的顺序列出首版字段映射。状态含义：

- `保留`：字段语义基本不变，只替换对象名或枚举值
- `重命名`：字段仍有对应职责，但名称改为 pipeline 语义
- `删除`：pipeline 不再需要该字段作为显式字段
- `新增`：pipeline 为 GitHub 项目判断新增的字段
- `改写`：字段位置可能保留，但维度和判断方式已经重写
- `拆分`：原字段职责被拆到多个 pipeline 字段或 Gate

### 1. `meta`

| Window Gate 字段 | Pipeline 字段 | 状态 | 说明 |
|---|---|---|---|
| `meta.opportunity_id` | `meta.hotspot_id` | 重命名 | 主键从机会编号改为热点编号，`OPP-xxx` 改为 `HSP-xxx`。 |
| `meta.opportunity_name` | `meta.hotspot_name` | 重命名 | 判断对象从 opportunity 改为 hotspot。 |
| `meta.owner` | `meta.owner` | 保留 | 负责人语义不变。 |
| `meta.date` | `meta.date` | 保留 | 记录日期语义不变。 |
| `meta.mode` | `meta.mode` | 保留 | `gut_check / express / full` 三档保留。 |
| `meta.status` | `meta.status` | 改写 | 状态集合从 `draft / watch / probe / build / drop` 改为 `draft / watch / probe / publish / drop`。 |
| `meta.review_cadence` | `meta.review_cadence` | 改写 | pipeline 默认 `weekly`，并删除 `quarterly`，因为热点和 repo 路由变化更快。 |

### 2. `decision_question` -> `hotspot_question`

| Window Gate 字段 | Pipeline 字段 | 状态 | 说明 |
|---|---|---|---|
| `decision_question` | `hotspot_question` | 重命名 | 顶层问题域从“机会决策问题”改为“热点是否值得转仓库”。 |
| `original_question` | `original_question` | 保留 | 原始问题仍然需要保留。 |
| `reframed_question` | `reframed_question` | 保留 | 重构后的问题仍然需要保留。 |
| `why_now_hypothesis` | `why_this_should_be_a_repo` | 改写 | 从“为什么现在”改为“为什么应该以 GitHub repo 承接”。 |
| `why_us_hypothesis` | 无 | 删除 | pipeline 首版不再显式判断“为什么是我们”，相关考虑后移到 repo 路由、维护能力和发布价值。 |
| `falsified_if` | `falsified_if` | 保留 | 反证条件保留。 |
| 无 | `source_context` | 新增 | 热点判断需要单独记录来源和脉络。 |
| 无 | `source_context.hotspot_source` | 新增 | 记录热点来源。 |
| 无 | `source_context.topic_lineage` | 新增 | 记录话题演化线索。 |
| 无 | `source_context.related_links` | 新增 | 记录相关链接，服务 repo 候选与证据回溯。 |

### 3. `gate_0_reframe`

| Window Gate 字段 | Pipeline 字段 | 状态 | 说明 |
|---|---|---|---|
| `gate_0_reframe` | `gate_0_reframe` | 保留 | Gate 0 仍然负责先把问题问对。 |
| `true_need` | `true_problem` | 重命名 | 从真实需求改为热点背后的真实问题。 |
| `alternatives` | `alternatives` | 保留 | 仍用于记录非当前方案的替代路径。 |
| `minimum_testable_unit` | `minimum_repo_unit` | 改写 | 最小可验证单元改为最小可交付仓库单元。 |
| `assumptions` | `assumptions` | 保留 | 假设列表保留。 |
| `unknowns` | `unknowns` | 保留 | 未知项保留。 |
| `verdict` | `verdict` | 保留 | `pass / hold / fail` 保留。 |

### 4. `gate_1_signal` -> `gate_1_hotspot_signal`

| Window Gate 字段 | Pipeline 字段 | 状态 | 说明 |
|---|---|---|---|
| `gate_1_signal` | `gate_1_hotspot_signal` | 重命名 | 从需求信号改为热点信号。 |
| `score` | `score` | 保留 | 评分语义保留。 |
| `dimensions.observable_pain` | `dimensions.signal_strength` | 改写 | 不再只看痛点可观察性，而看热点强度。 |
| `dimensions.user_workarounds` | 无 | 删除 | 用户自救方式不再是 pipeline 的核心判断项。 |
| `dimensions.behavioral_evidence` | `dimensions.behavior_evidence` | 重命名 | 行为证据保留，名称轻微收敛。 |
| `dimensions.structural_shift` | `dimensions.market_activity` | 改写 | 从结构性变化改为市场与技术活动。 |
| 无 | `dimensions.timing_window` | 新增 | 热点是否仍在窗口内需要显式评分。 |
| `dimensions.evidence_quality` | `dimensions.evidence_quality` | 保留 | 证据质量保留。 |
| `weakest_link` | `weakest_link` | 保留 | 最弱项保留。 |
| `evidence` | `evidence` | 保留 | 证据数组保留。 |
| `evidence[].type` | `evidence[].type` | 改写 | 枚举从用户/市场证据改为 `market_data / repo_activity / launch_signal / community_signal / field_observation / regulatory`。 |
| `evidence[].evidence_tier` | `evidence[].evidence_tier` | 保留 | 证据层级保留。 |
| `evidence[].summary` | `evidence[].summary` | 保留 | 摘要保留。 |
| `evidence[].confidence` | `evidence[].confidence` | 保留 | 置信度保留。 |
| `kill_signals` | `kill_signals` | 保留 | kill signals 保留。 |
| `verdict` | `verdict` | 保留 | 判定保留。 |

### 5. `gate_2_demand_window` -> `gate_2_project_shape`

| Window Gate 字段 | Pipeline 字段 | 状态 | 说明 |
|---|---|---|---|
| `gate_2_demand_window` | `gate_2_project_shape` | 改写 | 从需求窗口判断改为 GitHub 项目形态判断。 |
| `score` | `score` | 保留 | 评分保留。 |
| 无 | `project_shape` | 新增 | 新增核心枚举：`undecided / demo / starter / sdk / cli / agent_workflow / mcp_server`。 |
| `dimensions.urgency` | 无 | 删除 | 紧迫性不再作为 project shape 的直接维度。 |
| `dimensions.frequency` | `dimensions.audience_clarity` | 改写 | 从频率改成受众清晰度。 |
| `dimensions.pain_intensity` | `dimensions.deliverable_clarity` | 改写 | 从痛感强度改成可交付物清晰度。 |
| `dimensions.willingness_to_switch_or_pay` | `dimensions.reuse_potential` | 改写 | 从付费/切换意愿改成仓库复用潜力。 |
| `dimensions.external_tailwind` | `dimensions.novelty_fit` | 改写 | 从外部顺风改成热点新颖性与项目形态的匹配。 |
| 无 | `dimensions.scope_control` | 新增 | GitHub 项目需要显式控制仓库范围。 |
| `weakest_link` | `weakest_link` | 保留 | 最弱项保留。 |
| `evidence` | `evidence` | 保留 | 证据数组保留，但证据类型枚举改写。 |
| `window_assessment` | 无 | 删除 | 项目形态 Gate 不再输出窗口阶段。 |
| `kill_signals` | `kill_signals` | 保留 | kill signals 保留。 |
| `verdict` | `verdict` | 保留 | 判定保留。 |

### 6. `gate_3_product_window` -> `gate_3_repo_routing`

| Window Gate 字段 | Pipeline 字段 | 状态 | 说明 |
|---|---|---|---|
| `gate_3_product_window` | `gate_3_repo_routing` | 改写 | 从产品窗口判断改为仓库承接路线判断。 |
| `score` | `score` | 保留 | 评分保留。 |
| 无 | `repo_strategy` | 新增 | 新增核心枚举：`undecided / fork_existing / template_clone / new_repo`。 |
| 无 | `candidate_repos` | 新增 | 记录候选上游仓库或模板。 |
| 无 | `candidate_repos[].name` | 新增 | 候选仓库名称。 |
| 无 | `candidate_repos[].url` | 新增 | 候选仓库链接。 |
| 无 | `candidate_repos[].fit_reason` | 新增 | 为什么适合该路线。 |
| 无 | `candidate_repos[].license_note` | 新增 | 许可证与复用风险说明。 |
| `dimensions.tech_readiness` | `dimensions.implementation_speed` | 改写 | 技术 ready 程度转为路线实现速度。 |
| `dimensions.distribution_access` | 无 | 删除 | 分发渠道不再属于 repo routing 的核心维度。 |
| `dimensions.user_readiness` | `dimensions.upstream_fit` | 改写 | 用户 ready 改为上游仓库适配度。 |
| `dimensions.market_gap` | `dimensions.differentiation_room` | 改写 | 市场缝隙改为差异化空间。 |
| `dimensions.defensibility_potential` | `dimensions.maintenance_cost` | 改写 | 壁垒潜力改为维护成本。 |
| 无 | `dimensions.license_safety` | 新增 | GitHub 复用路线必须显式评估 license。 |
| `weakest_link` | `weakest_link` | 保留 | 最弱项保留。 |
| `evidence` | 无 | 删除 | 当前 pipeline 把候选仓库和路线字段作为主要证据容器。 |
| `window_assessment` | 无 | 删除 | repo routing 不再输出产品窗口阶段。 |
| `kill_signals` | `kill_signals` | 保留 | kill signals 保留。 |
| `verdict` | `verdict` | 保留 | 判定保留。 |

### 7. `gate_4_action` -> `gate_4_buildability` + `gate_5_publish_decision`

| Window Gate 字段 | Pipeline 字段 | 状态 | 说明 |
|---|---|---|---|
| `gate_4_action` | `gate_4_buildability` | 拆分 | 原 Action Gate 的“是否能行动”部分改为 buildability。 |
| `gate_4_action` | `gate_5_publish_decision` | 拆分 | 原 Action Gate 的“是否值得下注”部分改为发布决策。 |
| `score` | `gate_4_buildability.score` | 保留 | buildability 评分。 |
| `score` | `gate_5_publish_decision.score` | 拆分 | publish readiness 另行评分。 |
| 无 | `gate_4_buildability.build_commands` | 新增 | repo 是否能落地必须记录 install/build/test 命令。 |
| 无 | `build_commands.install` | 新增 | 安装命令。 |
| 无 | `build_commands.build` | 新增 | 构建命令。 |
| 无 | `build_commands.test` | 新增 | 测试命令。 |
| `dimensions.founder_edge` | 无 | 删除 | founder edge 不再作为 pipeline YAML 的显式维度。 |
| `dimensions.resource_fit` | `gate_4_buildability.dimensions.dependency_clarity` | 改写 | 资源匹配改成依赖清晰度。 |
| `dimensions.opportunity_cost` | `gate_5_publish_decision.dimensions.maintenance_readiness` | 改写 | 机会成本改成发布后的维护准备度。 |
| `dimensions.compounding_potential` | `gate_5_publish_decision.dimensions.public_value` | 改写 | 复利潜力改成公开仓库价值。 |
| `dimensions.speed_to_probe` | `gate_4_buildability.dimensions.local_build_feasibility` | 改写 | probe 速度改成本地构建可行性。 |
| 无 | `gate_4_buildability.dimensions.testability` | 新增 | 是否可测试。 |
| 无 | `gate_4_buildability.dimensions.docs_reproducibility` | 新增 | 文档是否可复现。 |
| 无 | `gate_4_buildability.dimensions.private_dependency_risk` | 新增 | 私有依赖风险。 |
| 无 | `gate_4_buildability.probe_requirements` | 新增 | 构建探针前置条件。 |
| 无 | `gate_5_publish_decision.dimensions.repo_readiness` | 新增 | 仓库达到发布标准的程度。 |
| 无 | `gate_5_publish_decision.dimensions.explainability` | 新增 | README、示例和代码是否解释得清。 |
| 无 | `gate_5_publish_decision.dimensions.distribution_value` | 新增 | 发布后是否有传播和承接价值。 |
| `weakest_link` | 两个 Gate 的 `weakest_link` | 拆分 | 分别记录构建最弱项和发布最弱项。 |
| `evidence` | 无 | 删除 | 当前两个 Gate 用命令、probe requirements 和维度承载证据。 |
| `kill_signals` | 两个 Gate 的 `kill_signals` | 拆分 | 分别记录 build kill signal 和 publish kill signal。 |
| `verdict` | 两个 Gate 的 `verdict` | 拆分 | buildability 与 publish decision 各自判定。 |

### 8. `decision`

| Window Gate 字段 | Pipeline 字段 | 状态 | 说明 |
|---|---|---|---|
| `decision.final_status` | `decision.final_status` | 改写 | 终态枚举从 `watch / probe / build / drop` 改为 `watch / probe / publish / drop`。 |
| `decision.summary` | `decision.summary` | 保留 | 总结保留。 |
| `decision.core_bet` | `decision.one_line_thesis` | 重命名 | 核心下注改成一句话判断，更贴近 publish memo。 |
| `decision.timing_verdict` | 无 | 删除 | 不再作为顶层决策字段。 |
| `decision.primary_constraint` | `decision.primary_constraint` | 改写 | 枚举从 `demand_side / supply_side / team_side / unknown` 改为 `hotspot_signal / project_shape / repo_routing / buildability / publish_readiness / unknown`。 |
| `decision.why_now` | `decision.why_now` | 保留 | 为什么现在仍保留。 |
| `decision.why_not_now` | `decision.why_not_now` | 保留 | 为什么不是现在仍保留。 |
| `decision.next_action` | `decision.next_action` | 保留 | 下一步动作保留。 |
| `decision.next_review_date` | `decision.next_review_date` | 保留 | 复查日期保留。 |
| `decision.kill_triggers_resolved` | `decision.kill_triggers_resolved` | 保留 | kill trigger 状态保留。 |
| `decision.veto_from_gate` | `decision.veto_from_gate` | 改写 | 可 veto 的 Gate 列表改成 pipeline Gate。 |
| `decision.abandon_reason` | `decision.abandon_reason` | 保留 | 放弃原因保留。 |

### 9. `probe_plan` -> `build_probe`

| Window Gate 字段 | Pipeline 字段 | 状态 | 说明 |
|---|---|---|---|
| `probe_plan` | `build_probe` | 重命名 | 最小实验从机会 probe 改为构建 probe。 |
| `hypothesis` | `hypothesis` | 保留 | 假设保留。 |
| `source_gate` | `source_gate` | 改写 | 来源 Gate 限定为 `gate_3_repo_routing / gate_4_buildability / gate_5_publish_decision`。 |
| `experiment_type` | `experiment_type` | 改写 | 实验类型改为 `local_clone_probe / template_probe / build_probe / test_probe / packaging_probe`。 |
| `cost` | `cost` | 保留 | 成本保留。 |
| `timebox` | `timebox` | 保留 | 时间盒保留。 |
| `success_signal` | `success_signal` | 保留 | 成功信号保留。 |
| `failure_signal` | `failure_signal` | 保留 | 失败信号保留。 |

### 10. `notes`

| Window Gate 字段 | Pipeline 字段 | 状态 | 说明 |
|---|---|---|---|
| `notes.contrarian_views` | `notes.contrarian_views` | 保留 | 反方观点保留。 |
| `notes.adjacent_signals` | 无 | 删除 | 邻近信号已由 `source_context.topic_lineage` 和 `related_links` 承接。 |
| `notes.open_questions` | `notes.open_questions` | 保留 | 开放问题保留。 |

### 11. `review_log`

| Window Gate 字段 | Pipeline 字段 | 状态 | 说明 |
|---|---|---|---|
| `review_log[].date` | `review_log[].date` | 保留 | 复查日期保留。 |
| `review_log[].previous_status` | `review_log[].previous_status` | 改写 | 状态枚举改为 pipeline 状态。 |
| `review_log[].new_status` | `review_log[].new_status` | 改写 | 状态枚举改为 pipeline 状态。 |
| `review_log[].what_changed` | `review_log[].what_changed` | 保留 | 变化说明保留。 |
| `review_log[].lessons` | `review_log[].lessons` | 保留 | 经验记录保留。 |

## 字段级迁移判断

从字段级看，pipeline 已经不只是把 `window-gate` 换名，而是完成了三类结构性迁移：

1. 入口迁移：`opportunity` 体系整体换成 `hotspot` 体系，并新增 `source_context`。
2. 中段迁移：`demand_window / product_window` 不再保留，改成 `project_shape / repo_routing`。
3. 终局迁移：`action` 被拆成 `buildability / publish_decision`，让 “能跑” 和 “值得发布” 分开判断。

后续如果要继续推进模板，可以优先做两件事：

- 给 `pipeline-gate.template.yaml` 补少量注释，解释新增枚举的使用边界。
- 把 `scaffold_pipeline.py` 的参数和字段级映射保持同步，避免脚手架只覆盖一部分核心字段。

## 当前结论

目前可以把 `hotspot-to-github-pipeline` 视为：

1. 复用 `window-gate-framework` 的工作流骨架。
2. 把中后段 Gate 全部改写为 GitHub 项目落地语义。
3. 用 `buildability + publish_decision` 替换原版的单一 `action` 终局。

因此，框架级迁移已经成立，字段级首版映射也已经可用。后续工作可以从“补模板注释”和“增强脚手架预填字段”两条线继续推进。
