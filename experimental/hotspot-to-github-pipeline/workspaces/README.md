# Workspaces

这个目录用于存放执行层产生的本地工作区。

典型用途：

- `probe` 模式下 clone 模板仓库
- `probe` 模式下新建最小 repo 骨架
- `publish` 模式下准备待推送的本地仓库

建议规则：

- 一个执行 run 使用一个独立子目录
- 默认命名使用 `HSP-001-YYYY-MM-DD-slug`
- 不要把运行时生成的临时文件当作模板手工编辑
