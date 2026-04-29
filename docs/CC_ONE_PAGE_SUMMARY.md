# AgentFlow 1-Page Summary

## 项目一句话

`agentflow-article-publishing` 是一个面向单人创作者的本地优先内容工作流：把“热点发现 → 自动成稿 → 局部人工修改 → 多平台预览与发布”压缩成 ~90min 一条可恢复链路。

## 当前形态（skill-first）

当前项目的交付形态是 **skill-first + `af` CLI**，不是 Web 应用：

- 日常全流程运行在 Claude Code 里，由 `.claude/skills/` 下 5 个 skill 编排
- 所有能力真正落地在 `af` 这个 Python CLI（`backend/agentflow/cli/commands.py`）
- 早期 Next.js + FastAPI 版本已整体归档到 `_legacy/`，不再是主路径

### skill 清单

| skill | 触发时机 | 作用 |
|---|---|---|
| `/agentflow` | 入口总览 | 说明日常 flow，引导到具体 skill |
| `/agentflow-style` | 每周一次 | D0：从 3-5 篇过去文章学 voice profile |
| `/agentflow-hotspots` | 每天 | D1：扫 Twitter/RSS/HN → 聚类 → 选 hotspot + angle |
| `/agentflow-write` | 每篇一次 | D2：skeleton → auto-fill → 交互式编辑循环 |
| `/agentflow-publish` | 每篇一次 | D3+D4：平台 preview → 图片处理 → 发布 |

## 已完成能力

### 核心链路（D0–D4）

- **D0** 风格学习：从样稿生成 `style_profile.yaml`，所有 agent 都读它
- **D1** 热点扫描：Twitter / RSS / HN 采集 → 聚类 → 独立观点挖掘
- **D2** 写作：skeleton-first → auto-fill（默认）→ 自然语言局部编辑（`改短` / `加例子` / `改锋利` / `去AI味` / `展开`）
- **D3** 多平台适配：同一 draft 针对 Ghost / LinkedIn / Medium 分别生成 `platform_versions/*.md`
- **D4** 多平台发布：顺序调用各平台 publisher，失败单独记录，不阻塞

### `af` CLI 完整覆盖

```
af learn-style / hotspots / write / fill / edit / image-resolve
af draft-show / preview / publish / memory-tail / run-once
```

### 平台与状态

- 支持 `ghost_wordpress`（主）、`linkedin_article`（可选）、`medium`（deprecated，2025-01-01 官方关停）
- 文章状态推导：`approved → skeleton_ready → draft_ready → preview_ready → published`

### 记忆层

- 统一事件流：`~/.agentflow/memory/events.jsonl`（append-only）
- 已记录事件：`article_created / fill_choices / section_edit / hotspot_review / preview / publish / learn_style / image_resolved`

## 当前技术方案

- 语言/运行时：Python 3.11+（bundle 的 `.venv` 是 3.14）
- CLI 框架：Click（`af` 入口）
- 运行态数据：本地文件系统 `~/.agentflow/`
- 默认开发模式：`MOCK_LLM=true`（所有 LLM/embedding/publisher 走 fixtures）
- 关键设计：
  - skill 只做编排，重逻辑留在 `af`
  - 单篇状态写 `metadata.json`；跨篇行为单独写 `memory/events.jsonl`
  - Provider 可切换：生成走 Kimi 或 Claude；embedding 走 Jina 或 OpenAI

## 已验证内容

### mock 端到端（2026-04-24）

- `af hotspots --json` → 5 个 hotspots，3 个 collectors 都走 mock ✓
- `af write <hid> --auto-pick --json` → `skeleton` + `draft` 齐全 ✓
- `af preview <aid> --json` → 生成 `ghost_wordpress.md` + `linkedin_article.md` ✓
- `af publish <aid> --force-strip-images --json` → 两个平台 `status=success` ✓
- `af memory-tail` → `article_created / fill_choices / preview / publish` 事件都在 ✓

产物齐全：`~/.agentflow/drafts/<aid>/{skeleton.json, draft.md, metadata.json, d3_output.json, platform_versions/*.md}`。

### 实 Key 端到端（2026-04-24）

- D0 `af learn-style --dir samples/` → Moonshot Kimi 读 2 样本 → 6 per-article analyses → 新 `style_profile.yaml` ✓
- D1 `af hotspots` → Twitter+RSS+HN 采 244 signals → Jina v3 聚 8 clusters → Kimi 生成 topic + angles ✓
- D2 `af write --auto-pick` → Kimi K2.6 输出 3/3/4/3 skeleton + auto-fill 1812 字 draft ✓
- D3 `af preview` → Kimi 生成 Ghost + LinkedIn 两平台版本 ✓
- D4 `af publish`（`GHOST_STATUS=draft`） → Ghost 后台真建 draft，`platform_post_id` 回传 ✓
- D4 `af publish-rollback --post-id` → Ghost DELETE 真删，Ghost API 直接 probe 返回 404，drafts 计数 0 ✓

LinkedIn 未过实 key（缺 OAuth token），Medium 已 deprecated。

### Skill 流程 Step 1b（pre-publish overview）实跑验证

在 `hs_20260423_003-20260423174103-44d3cf57`（主题：意识独立性；Kimi 起的标题：「量子纠缠：意识独立存在的科学钥匙？」）上 dry-run Step 1b，输出：

```
Lineage:     hs_20260423_003 → angle #0 → series A
Topic:       意识是否独立于物理实体存在
Content:     4 sections, 1872 words
Compliance:  avg 0.81; 1 section < 0.85 (worst: "意识与量子纠缠的联系" at 0.70)
Tags:        量子纠缠, 意识独立存在的科学钥匙, ...
Images:      0 unresolved
Platforms:   ready: ghost_wordpress   will skip: linkedin_article (missing credentials)

References (top 3):
  1. [twitter @VitalikButerin] ... "consciousness is rooted in some parts of physics..."
  2. [twitter @VitalikButerin] ... "infinite identical minds have to be part of identical worlds..."
  3. [twitter @VitalikButerin] ... "consciousness is substrate-independent..."
```

**Step 1b 直接暴露了一次 hallucination**：3 条 references 全是 Vitalik 谈 substrate independence，**从头到尾没有「量子纠缠」这个词**——Kimi 把量子纠缠作为修辞包装塞进了标题和结构。没有 Step 1b（我之前 ad-hoc 汇总）的时候，发稿前看不出来这条。这是 skill 真正帮用户把关的证据。

## 当前边界

这不是一个“全自动无人值守发布系统”，而是一个“AI 先成稿，人做局部控制”的工作台：

- `af run-once` 只是最小编排（D1 → 交接），不是后台任务系统
- 记忆层目前只做记录，不做默认策略回写
- 持久化仍以文件系统为主，未引入数据库
- 无 async infra、无 UI、无多用户
- LinkedIn 实 key 路径缺 OAuth token，尚未过实网
- 图片支路（`image-resolve`）未在真实文章上过——当前生成文章都 `image_placeholders=0`

## 下一阶段最重要的 3 件事

1. **Real-Key Readiness**（进行中）
   - `MOCK_LLM=false` 全链路 smoke：Kimi/Claude 生成、Jina/OpenAI embedding、Ghost/LinkedIn 发布
   - credential health 检查、source management、失败样本回归
2. **Memory → Default Strategy**
   - 让历史 `fill_choices / section_edit / publish` 真正影响默认标题、默认平台、默认编辑偏好
3. **Async & Safety**
   - 后台任务、进度、版本历史、回滚

## 给 review 的关注点

### 重点 1：skill / CLI 契约是否稳定

- 每个 skill 调用的 `af <subcommand> --json` 输出 schema 是否稳定
- 错误语义（409 unresolved images、404 missing article、400 bad input）是否和 skill prompt 里的期望一致

### 重点 2：状态与记忆层边界是否清晰

- `metadata.json` 只承载单篇状态
- `events.jsonl` append-only，不污染单篇数据
- `publish_history.jsonl` 每次发布独立一行，失败也记

### 重点 3：写作 / 预览 / 发布状态是否有覆盖风险

- 重新 fill、局部 edit、preview、publish、retry 之间是否可能打架
- `force_strip_unresolved_images` 拦截逻辑是否清楚

### 重点 4：当前 v0.1 边界是否合理

- 接受“mock-first 验收 + 本地优先 + 无 async infra + 无 Web UI”

## 推荐复现（mock 一条龙）

```bash
cd backend && source .venv/bin/activate

MOCK_LLM=true PYTHONPATH=. af hotspots --json > /tmp/out.json
HID=$(python -c 'import json; print(json.load(open("/tmp/out.json"))["hotspots"][0]["id"])')

MOCK_LLM=true PYTHONPATH=. af write "$HID" --auto-pick --json > /tmp/art.json
AID=$(python -c 'import json; print(json.load(open("/tmp/art.json"))["article_id"])')

MOCK_LLM=true PYTHONPATH=. af preview "$AID" --json >/dev/null
MOCK_LLM=true PYTHONPATH=. af publish "$AID" --force-strip-images --json
MOCK_LLM=true PYTHONPATH=. af memory-tail --limit 5 --json
```

## 文档入口

- 总体 PRD：`docs/PRD_OVERVIEW.md`
- 技术方案：`docs/SOLUTION_OVERVIEW.md`
- 项目 README（安装 / CLI 参考 / 平台配置）：`README.md`
- review 交接：`CCREVIEW_HANDOFF.md`
- 旧 Web UI 形态（归档，非主路径）：`_legacy/`
