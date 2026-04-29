# Open Claw Working Template (v2)

## Purpose

为 AgentFlow framework 任务提供可选的 profile-aware 思考脚手架。
**复杂任务用此模板；简单任务跳过，正常对话即可。**

## User Profile（可选 input）

- **role**: 框架开发者 / 用户 / onboard 调试 / 其它
- **session intent**: 当前要解决的最小问题（一句话）
- **constraint**: 不能动什么 / 必须保留什么 / time-box

> 上述字段缺失时按"首次部署 / 初始化续跑"默认；只有 user 明确要求代码修改、review 或具体 Gate 排障时，才切到框架开发者视角。

## Open Claw Working Template（5 段；复杂任务可选 / 简单任务跳过）

### 1) Discover

- 我需要确认哪些事实？grep / read 哪个最少集？
- `~/Desktop/agentflow-status.md` 里相关批次是哪一批？
- 已知陈旧信息（5-state 模型、品牌硬编码）需要剔除吗？

### 2) Decide

- 改动落在 framework / 用户数据 / onboard 哪一层？
- 是否触及 hard rules（brand-neutral / metadata-vs-events / mock-vs-real-key）？
- 最小可验证切片（MVS）是什么？

### 3) Act

- 仅改必要文件；保持 brand-neutral
- 触及 state.py / daemon.py 时同步检查 state_machine.md 和 TG_BOT_FLOWS.md 是否需要联动

### 4) Verify

- `pytest tests/test_v02_workflows.py -q` 应 42+ passed
- 涉及凭据 / daemon 时跑 `af doctor`
- mock pass 不等于 real-key ready

### 5) Report

- 改了哪些文件 / 为什么 / 风险
- 如果有 follow-up，写入 `~/Desktop/agentflow-status.md` 新批次

---

> 不需要每次都写满 5 段。短任务一行结论即可；复杂任务再展开。
