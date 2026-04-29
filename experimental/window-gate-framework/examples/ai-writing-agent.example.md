# Example: AI Writing Agent Opportunity

## 1. Conclusion

`Decision`: `probe`

`One-line thesis`: AI 写作与分发代理的需求信号真实，但产品窗口是否足够宽、是否能形成非工具化壁垒，还需要低成本验证。

`Confidence`: `medium`

`Timing verdict`: `now`

`Primary constraint`: `supply_side`

## 2. Opportunity

- Opportunity ID: OPP-001
- Opportunity: 面向个体内容创作者的小型写作与发布代理
- Original question: 要不要做一个 AI 写作产品？
- Reframed question: 当前是否存在一个足够强的“内容生产与多平台分发效率痛点”，值得被做成一个可复利的代理工作流产品？
- Why now: 模型质量、长上下文、工具调用和多平台发布需求正在同一时间成熟
- Why us: 如果团队对创作流程、分发环节和 prompt/workflow 设计有长期积累，可以先从 workflow alpha 切入
- Falsified if: 用户只把它当作一次性玩具，不会持续复用

## 3. Gate Summary

| Gate | Verdict | Score | Why it passed / held / failed |
|---|---:|---:|---|
| Reframe | pass | n/a | 问题从“做 AI 写作产品”重构成“做内容生产工作流产品”后更清晰 |
| Signal | pass | 4 / 5 | 用户确实在用 ChatGPT、Notion、手工分发等低效方式自救 |
| Demand Window | pass | 4 / 5 | 创作者对提效和跨平台改写有持续需求，且需求强度在上升 |
| Product Window | hold | 3 / 5 | 模型能力够用，但差异化和稳定留存仍未被证明 |
| Action | pass | 4 / 5 | 小团队可以快速做 workflow probe，成本可控 |

## 4. Strongest Evidence

1. 用户已经在用多个通用工具手工拼接工作流，说明痛点真实。
2. 多平台内容改写、封面、发布动作天然适合 agent 化。
3. 现在的模型能力首次让“草稿到分发”链条变得足够可用。

## 5. Biggest Risks

1. 用户只为单次提效买单，不形成高频留存。
2. 通用大模型产品快速内置相同能力，压缩独立产品空间。
3. 真实瓶颈可能不在写作，而在选题、渠道与受众增长。

## 6. Kill Signals

- 如果用户复购或周活显著不足，说明不是持续工作流需求。
- 如果用户愿意用通用模型凑合而不愿迁移，说明切换价值不够大。
- 如果获客严重依赖高成本内容教育，说明产品窗口未真正打开。

`Veto from gate`: `Product Window`

## 7. Decision Logic

### Why this may work

- 需求并不是“要一个写作机器人”，而是“要一个稳定缩短产出链路的工作系统”。
- 如果能把热点、写作、改写、发布串成闭环，价值会高于单点写作助手。

### Why this may fail

- 用户未必愿意长期把内容生产主流程交给一个新工具。
- 分发侧的平台规则和账号资产，可能比写作侧更难产品化。

### Why now / why not now

- 现在适合做 probe，因为能力条件开始具备。
- 现在还不适合重投入 build，因为真正壁垒还没有被验证。

## 8. Next Step

`Immediate action`: `run probe`

`Timebox`: `14 days`

`Owner`: `founder`

`Success signal`: 至少 5 位目标用户连续两周复用同一条工作流

`Failure signal`: 试用后只保留“单次润色”用法，没有跨步骤复用

`Next review date`: `T+14 days`

## 9. Review Delta

`Previous status`: `watch`

`What changed since last round`: 从机会观察升级为结构化验证，并明确了最小实验。

`What remains unresolved`: 留存、差异化和分发效率是否足够强。

`Lesson so far`: 这个方向值得 probe，但还不值得直接重投入 build。
