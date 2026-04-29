# Cases

这个目录用于存放每个热点到 GitHub 项目的工作副本。

推荐结构：

- 一个热点判断一个子目录
- 目录名使用 `HSP-001-YYYY-MM-DD-slug`
- 每个目录至少包含：
  - `01-hotspot-intake.md`
  - `02-pipeline-gate.yaml`
  - `03-publish-decision-memo.md`
  - `04-build-probe-run.md`
  - `05-review-checkpoint.md`
- 目录内所有文件都应与 `pipeline-pool.md` 中的同一个 `Hotspot ID` 对齐

推荐工作方式：

- 先运行脚手架生成 case 目录
- 在 `pipeline-pool.md` 中查看该热点的总索引
- 在 case 目录中推进 intake、gate、memo、build probe、review
- 每次状态变化后，先更新 case，再更新 pipeline pool 的一行摘要

可以用下面的命令快速生成一套初始文件：

```bash
python experimental/hotspot-to-github-pipeline/scaffold_pipeline.py --hotspot-name "Your Hotspot"
```
