# Hotspot To GitHub Pipeline Spec

## Purpose

这套 pipeline 用来把一个“行业热点”推进成一个可以被 GitHub 承接的项目判断与执行流程。

它分成两层：

1. 决策层：判断值不值得做、做成什么、路由到哪类 repo
2. 执行层：按判断结果做发现、probe、发布与结果回写

## Core Artifacts

- `01-hotspot-intake.md`
- `02-pipeline-gate.yaml`
- `03-publish-decision-memo.md`
- `04-build-probe-run.md`
- `05-review-checkpoint.md`

## Gate Semantics

- `Gate 0: Reframe`
- `Gate 1: Hotspot Signal`
- `Gate 2: Project Shape`
- `Gate 3: Repo Routing`
- `Gate 4: Buildability`
- `Gate 5: Publish Decision`

## Execution Model

执行层由 `run_pipeline.py` 承接，支持四种模式：

### `discover`

自动用 GitHub 搜索 candidate repos。

行为：

- 生成搜索 query
- 调用 `gh search repos`
- 归一化候选仓库
- 为候选仓库生成可解释分数与排序原因
- 生成 `recommended_strategy`
- 在 `--execute` 时写回 `02-pipeline-gate.yaml`

### `inspect`

只读取 `02-pipeline-gate.yaml` 并输出执行计划，不修改任何内容。

### `probe`

根据 `repo_strategy` 准备本地 workspace，并尝试运行：

- install
- build
- test

默认 dry-run，只有加 `--execute` 才真正执行。

### `publish`

在 `probe` 的基础上，尝试执行 GitHub 发布动作。

安全约束：

- 必须显式传 `--execute`
- 必须显式传 `--allow-publish`

策略说明：

- `fork_existing`: 调用 `gh repo fork`
- `template_clone`: clone 上游，重置 git 历史后创建新 repo
- `new_repo`: 在本地 workspace 初始化 git 仓库，再调用 `gh repo create`

## Repo Plan

`pipeline-gate.yaml` 中的 `repo_plan` 用于执行层：

- `local_workspace`
- `repo_name`
- `github_owner`
- `visibility`
- `default_branch`

如果这些字段为空，执行层会用合理默认值推导。

## Automatic Writeback

当执行层使用 `--execute` 且未传 `--no-writeback` 时，会自动回写：

### 1. `02-pipeline-gate.yaml`

包括：

- `candidate_repos`
- `discovered_query`
- `recommended_strategy`
- `recommended_reason`
- `execution_state.discovery`
- `execution_state.probe`
- `execution_state.publish`

### 2. `04-build-probe-run.md`

包括：

- 实际命令
- build/test 状态
- 推荐下一状态
- 观察与总结

### 3. `pipeline-pool.md`

自动更新当前 `Hotspot ID` 对应的一行摘要。

### 4. `03-publish-decision-memo.md`

同步更新：

- routing decision
- next step
- latest review delta

### 5. `05-review-checkpoint.md`

同步更新：

- latest review meta
- changed evidence
- project shape / repo strategy
- next review date

## Workspace Rules

默认 workspace 根目录是：

- `experimental/hotspot-to-github-pipeline/workspaces/`

单次运行目录建议为：

- `HSP-001-YYYY-MM-DD-slug`

## Safety Defaults

- 默认不执行，只打印计划
- 默认不发布，除非显式允许
- 发现 workspace 已存在时直接报错，避免覆盖
- 发现 git 用户身份未配置时拒绝 publish

## Current Scope

当前版本已经能：

- 自动抓取 candidate repos
- 对 candidate repos 做可解释打分与排序
- 读取 pipeline case
- 准备本地 workspace
- 执行 install/build/test
- 自动回写 gate / probe-run / memo / review / pool
- 在满足条件时调用 `gh` 进行真实发布

当前版本还没有：

- 根据 probe 结果自动重评分与自动推进全部状态
- 更强的 candidate repo 质量打分与排序逻辑

## Recommended Usage

建议按这个顺序使用：

1. 用 `scaffold_pipeline.py` 生成 case
2. 补全 `02-pipeline-gate.yaml`
3. 运行 `run_pipeline.py --mode discover`
4. 决定 candidate repo 与 `repo_strategy`
5. 运行 `run_pipeline.py --mode probe`
6. 根据结果决定是否 `publish`
