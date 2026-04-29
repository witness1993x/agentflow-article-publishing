# Cases

这个目录用于存放每个具体机会的工作副本。

推荐结构：

- 一个机会一个子目录
- 目录名使用 `OPP-001-YYYY-MM-DD-slug`
- 每个目录至少包含：
  - `01-opportunity-intake.md`
  - `02-window-gate.yaml`
  - `03-decision-memo.md`
  - `04-probe-run.md`
  - `05-review-checkpoint.md`
- 目录内所有文件都应与 `opportunity-pool.md` 里的同一个 `Opportunity ID` 对齐

推荐工作方式：

- 先运行脚手架生成 case 目录
- 在 `opportunity-pool.md` 中查看该机会的总索引
- 在 case 目录中推进 intake、gate、memo、probe、review
- 每次状态变化后，先更新 case，再更新机会池的一行摘要

可以用下面的命令快速生成一套初始文件：

```bash
python experimental/window-gate-framework/scaffold.py --name "Your Opportunity"
```
