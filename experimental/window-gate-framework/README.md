# Window Gate Framework

一个用于判断机会是否成立、是否处于可进入窗口、以及是否值得当前团队下注的抽象框架。

## 适用场景

- 创业方向判断
- 新产品线评估
- 需求窗口判断
- 产品窗口判断
- 进入时机判断
- 小团队资源下注决策

这个框架默认不回答“怎么做产品”，而是先回答：

1. 这是不是一个真实信号？
2. 这个需求是不是正在进入窗口？
3. 这个产品是不是现在可以成立？
4. 这件事是不是值得由我现在来做？

## 核心结构

这套框架分成两层：

- `Layer A: Framing`，用于先把问题问对
- `Layer B: Gates`，用于判断这个机会是否值得继续推进

其中 `Gate 0` 是前置 framing，不参与总分平均；`Gate 1-4` 才是可评分闸门。

### Layer A: Framing

#### Gate 0: Reframe Gate

先重构问题，避免在错误的问题上做精细分析。

关键问题：

- 原始问题是什么？
- 背后的真实需求是什么？
- 不做这个具体产品，还有哪些替代路径？
- 最小可验证单元是什么？
- 什么证据会推翻当前判断？

### Layer B: Gates

#### Gate 1: Signal Gate

判断问题是否真实存在，而不是情绪化热点。

关键问题：

- 是否存在可观察的重复痛点？
- 用户是否已经在用低效方式自救？
- 信号来自真实行为，还是只来自表达？
- 这个变化是短期噪音，还是结构性变化？
- 我们掌握的是一手证据，还是二手观点？

`Gate 1` 只回答“问题是否存在且可观察”，不回答“现在是不是非做不可”。

#### Gate 2: Demand Window Gate

判断需求窗口是否已经打开。

关键问题：

- 痛点是否高频、刚性、持续存在？
- 用户的“不满意旧方案”是否正在加速？
- 需求是否从“可有可无”变成“必须解决”？
- 是否有外部变化放大了需求强度？
- 用户是否愿意为更好方案付出切换成本、时间成本或金钱成本？

`Gate 2` 只回答“需求强度与窗口”，尽量少谈技术实现。

#### Gate 3: Product Window Gate

判断产品在当前时间点是否具备成立条件。

关键问题：

- 关键技术、基础设施或模型能力是否成熟到可交付？
- 分发渠道是否可用，获客成本是否还能承受？
- 用户教育成本是否已经下降？
- 市场是否 ready，但还没有被巨头完全锁死？
- 现在进入是太早、正好，还是太晚？

`Gate 3` 只回答“供给侧是否 ready”，包括技术、分发、用户 readiness 和市场缝隙。

#### Gate 4: Action Gate

判断是不是值得当前团队亲自下注。

关键问题：

- 为什么是你，而不是别人？
- 你是否有渠道、认知、资源或叙事上的相对优势？
- 如果现在做，需要放弃什么？
- 这是不是一个能积累壁垒的方向？
- 现在最合理的是观察、做 probe，还是直接 build？

`Gate 4` 只回答“为什么该由我们现在做”，包括团队优势、机会成本和行动路径。

## 决策输出

每个机会最终只落到四种状态之一：

- `watch`: 信号存在，但窗口未开，继续观察
- `probe`: 窗口可能形成，但关键假设未验证，先做低成本实验
- `build`: 窗口已形成，且团队适合下注，进入产品化
- `drop`: 证据不足或机会成本过高，停止投入

## 使用方法

1. 先用 `templates/opportunity-intake.template.md` 记录机会
2. 决定是否值得开一轮 Gate
3. 填写 `templates/window-gate.template.yaml`
4. 对 `Gate 1-4` 按 1-5 分打分，并记录证据
5. 明确 kill signal、timing verdict 和 next action
6. 用 `templates/decision-memo.template.md` 输出本轮结论
7. 如果结论是 `probe`，必须同步填写一个最小实验计划
8. 到 `next_review_date` 后，用复查模板更新判断

## 评分建议

- `1`: 明显不成立
- `2`: 证据很弱
- `3`: 有一定迹象，但不稳定
- `4`: 基本成立
- `5`: 强成立，且有可验证证据

建议规则：

- 任一 Gate 出现核心 kill signal，优先暂停而不是继续美化结论
- `Signal` 和 `Demand Window` 不成立，不进入产品设计
- `Product Window` 不成立，不进入正式 build
- `Action Gate` 不成立，可以保留机会，但不由当前团队下注

## 轻量决策规则

### Gate 评分

- `Gate 0` 不计入平均分，只看 `pass / hold / fail`
- `Gate 1-4` 的 `score` 建议由各自维度平均得到
- 如果某个维度没有足够证据，可以先留低分，并在 memo 里写清原因

### 一票否决

出现以下任一情况时，不能进入 `build`：

- `Gate 0 = fail`
- `Gate 1 = fail`
- `Gate 2 = fail`
- `Gate 3 = fail`
- `Gate 4 = fail`
- 已有明确触发的核心 `kill_signal`

### watch -> probe

通常满足以下条件时，可以从 `watch` 升到 `probe`：

- `Gate 0 = pass`
- `Gate 1` 至少有真实行为证据，而不只是主观看法
- `Gate 2` 显示窗口正在形成或已经打开
- 没有已触发的 kill signal
- 已经能设计一个低成本、可在短时间内完成的实验

### probe -> build

通常满足以下条件时，才考虑从 `probe` 升到 `build`：

- `Gate 1` 和 `Gate 2` 基本成立
- `Gate 3` 不是 `too_early` 或明显不可交付
- `Gate 4` 成立，且当前团队确实有优势和资源匹配
- probe 已拿到足够强的正向信号
- 没有未解决的一票否决项

### timing verdict

为了避免“需求窗口”和“产品窗口”分开写但没有汇总，每轮 memo 都建议额外给出一个总的时机判断：

- `too_early`
- `now`
- `late`

这个判断应综合 `Gate 2` 和 `Gate 3` 一起得出，而不是单看其中一个 Gate。

## 最小工作流

1. `intake`：先记机会，不急着深挖
2. `gate round`：做一轮结构化判断
3. `memo`：沉淀结论和风险
4. `probe`：如果需要，用最小实验验证关键假设
5. `review`：在约定时间回看，决定升、降、停
6. `pool`：把所有机会维护在一个轻量索引里

## 命名规范

为了让机会池、case 目录和各模板能稳定对齐，统一使用下面两层命名：

- `Opportunity ID`: `OPP-001`, `OPP-002`, `OPP-003`
- `Case folder`: `OPP-001-YYYY-MM-DD-slug`

建议规则：

- 一个机会只有一个主 `Opportunity ID`
- 每个 case 目录都带上同一个 `Opportunity ID`
- `slug` 只负责可读性，真正的主键是 `OPP-xxx`
- `opportunity-pool.md` 作为总索引，case 目录作为详细工作副本
- Intake、Gate YAML、Memo、Probe、Review 都写同一个 `Opportunity ID`

## 脚手架

如果你想快速为一个新机会生成完整工作目录，可以直接运行：

```bash
python experimental/window-gate-framework/scaffold.py --name "Your Opportunity"
```

常用参数：

```bash
python experimental/window-gate-framework/scaffold.py \
  --name "AI Research Copilot" \
  --owner "founder" \
  --status "watch" \
  --timing "unknown" \
  --thesis "先观察需求强度，再决定是否开 probe" \
  --question "Should we pursue AI Research Copilot?" \
  --output-dir experimental/window-gate-framework/cases
```

脚手架会自动生成：

- 统一的 `OPP-xxx` 编号
- 统一的 case 目录名
- `01-opportunity-intake.md`
- `02-window-gate.yaml`
- `03-decision-memo.md`
- `04-probe-run.md`
- `05-review-checkpoint.md`
- 同时自动向 `opportunity-pool.md` 追加一条索引

常用参数：

- `--status`: `draft / watch / probe / build / drop`
- `--timing`: `unknown / too_early / now / late`
- `--thesis`: 预填一条简短判断
- `--next-review-date`: 手动指定复查日期
- `--review-days`: 不手动指定时，自动用多少天后作为复查日期
- `--pool-file`: 指定机会池文件
- `--skip-pool`: 只生成 case，不写入机会池

## 文件说明

- `opportunity-pool.md`: 真正使用中的机会池总索引
- `templates/opportunity-intake.template.md`: 机会入口
- `templates/window-gate.template.yaml`: 一轮 Gate 判断的结构化记录
- `templates/decision-memo.template.md`: 一轮决策输出
- `templates/probe-run.template.md`: 实验执行记录
- `templates/review-checkpoint.template.md`: 周期复查
- `templates/opportunity-pool.template.md`: 多机会索引
- `scaffold.py`: 生成新机会工作目录的零依赖脚手架
- `cases/`: 每个真实机会的工作副本
- `examples/*.md|*.yaml`: 示例填法

## 目录结构

```text
experimental/window-gate-framework/
├── README.md
├── opportunity-pool.md
├── scaffold.py
├── cases/
│   └── README.md
├── templates/
│   ├── opportunity-intake.template.md
│   ├── window-gate.template.yaml
│   ├── decision-memo.template.md
│   ├── probe-run.template.md
│   ├── review-checkpoint.template.md
│   └── opportunity-pool.template.md
└── examples/
    ├── ai-writing-agent.example.md
    ├── ai-writing-agent.window-gate.example.yaml
    ├── creator-research-monitor.watch.example.md
    ├── browser-agent-for-everyone.drop.example.md
    └── vertical-compliance-copilot.build.example.md
```

## 环境说明

当前这套实验内容是文档与模板，不依赖运行环境，所以没有复制 `.env`。

如果后续你要把它扩展成脚本化评分器、机会库或自动研究流水线，再单独在这个目录下补 `.env.template` 会更合适。
