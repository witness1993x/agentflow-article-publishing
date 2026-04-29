# 图片素材插入博客文章 — 策略 MEMO

> Status: 策划中 | Updated: 2026-04-24 | Owner: D2 / D2.5 / D3 链路

## 1. 当前状态

**基础设施就绪但未触发**：

- `[IMAGE: description]` 语法已定义（`markdown_utils.py::_IMAGE_PLACEHOLDER_RE`）
- `ImagePlaceholder` model 已在 `shared/models.py`
- `af image-resolve <article_id> <placeholder_id> <file_path>` 命令已实现，把占位替换为 `![desc](path)`
- D4 publish 的 `--force-strip-images` 可在未解绑时强删占位
- D3 的 Ghost/LinkedIn/Medium adapter 都能处理带 `![]` 图片的 draft

**缺口**：**D2 生成 draft 时从不插 `[IMAGE:]` 占位**。D2 的 prompts (`d2_skeleton_generation.md` / `d2_paragraph_filling.md`) 没提图片。所以当前所有文章都是 `image_placeholders = []`，`image-resolve` 永远无用武之地。

## 2. 问题分解

博客文章配图是一条完整的子流程，涉及四件事：

1. **决定要不要插图**（是/否，多少张，在哪里）
2. **决定插什么图**（图源：本地库 / AI 生成 / 从 reference 抓 / 用户指定）
3. **让图片真的落到 draft 里**（从占位 → 图片路径/URL → 平台最终文本）
4. **多平台适配**（Ghost 宽松、LinkedIn 受限、Twitter 图片尺寸规则）

当前系统只解决了 (3) 的下半段（`image-resolve`）。其他三件事要设计。

## 3. 设计目标

- **图片是内容的一等公民**，不是事后贴的装饰
- **插图时机可控**：AI 默认建议，用户拍板
- **不锁死图源**：本地库 / 生成 / 抓图 / 用户给都能走同一套 placeholder 机制
- **不破坏现有 `af image-resolve`**：placeholder 生成后的解析路径不变
- **多平台差异化**：同一篇文章在 Ghost 可能插 3 张，LinkedIn 可能 1 张，Twitter 可能 0 张

## 4. 四件事分别的选型

### 4.1 决定要不要插图 —— D2.5（推荐）

| 时机 | 优点 | 缺点 | 选？ |
|---|---|---|---|
| A. D2 section_filler 每段生成时自行决定 | 贴合上下文 | prompt 复杂、每段各自为政、LLM 难以把握全文图片节奏 | ❌ |
| B. D2 生成 skeleton 时声明每 section 的"图需求" | 全局视角 | 太早，还没生成正文就定图 | 不理想 |
| **C. 独立一步 D2.5 `af propose-images`** | 扫描完整 draft、全局平衡图片节奏、占位单独落盘 | 多一步命令 / LLM 调用 | ✓ |
| D. 发布前 Step 1b 里提醒 | 用户视角 | 时机太晚，要回去改 draft 很痛 | 仅作为兜底提醒 |

**推荐 C + D 兜底**：独立 `af propose-images` 命令，产出带 `[IMAGE:]` 的 draft，默认在 `af write --auto-pick` 成功后自动调用一次。用户可 `--skip-images` 绕过。

### 4.2 决定插什么图 —— 图源分层

| 图源 | 适用场景 | 成本 | 实现复杂度 |
|---|---|---|---|
| 1. 本地图库 `~/Pictures/agentflow/` | 截图、示意图、用户已有素材 | 零 | 低（字符串匹配 description + fuzzy） |
| 2. AI 生成（DALL·E / SD / Flux） | 概念图、抽象封面、非写实场景 | 有（API 费） | 中（新 provider 接口） |
| 3. 从 hotspot.source_references 抓 | 引用原推/文章的原图（如 Vitalik 的 tweet 有图） | 零 | 中（HTTP 抓 + 本地缓存） |
| 4. 用户现场上传 | 兜底 | 零 | 已完成（`af image-resolve`） |
| 5. 公共图源 API（Unsplash / Pexels） | 通用题材 | 免费 tier 够用 | 中 |

**v0.1 推荐组合**：1 + 4（本地 + 手动）。2/3/5 放 v0.5+。

`preferences.yaml`（见 Memory MEMO）的 `image.resolved_path_prefixes` 可直接用作本地图库位置。

### 4.3 让图片真的进 draft —— 不改现有机制

`[IMAGE: description]` 占位不变。D2.5 只负责**往 draft 里插占位**，不负责解析。

落盘后仍然由 `af image-resolve` 完成最后一步（手动）或新增的 `af image-auto-resolve`（自动扫图库匹配）。

### 4.4 多平台适配 —— D3 平台规则

每个 adapter 增加 `image_policy`:

```yaml
ghost_wordpress:
  max_images: 10
  preferred_image_count: 3-5       # 长文
  feature_image: true              # 用第一张图作 feature_image
  allow_inline: true

linkedin_article:
  max_images: 5
  preferred_image_count: 1-2
  feature_image: true              # 最靠前的一张作 cover
  allow_inline: true
  caption_required: false

medium:
  max_images: 10
  allow_inline: true
  first_image_is_feature: true
  # Medium 已 deprecated，维持兼容即可
```

D3 adapter 按 `image_policy.max_images` 裁剪：若 draft 有 8 张但 LinkedIn max 是 5 → 保留全文最靠前的 5 张 + 丢弃尾部。用户在 Step 1b 能看到 "Ghost: 8 images; LinkedIn: 5 (3 dropped)"。

## 5. 新的端到端流程

```
/agentflow-write <hid>
   ├── af write --auto-pick              （现有）
   ├── af propose-images <aid>           （新 D2.5）
   │     └─ LLM 读完整 draft + hotspot refs
   │         → 建议 N 个插图位 + description
   │         → 写回 draft.md 带 [IMAGE: ...]
   │         → 写 `proposed_images` 事件
   │
   ├── af image-auto-resolve <aid>       （新，可选）
   │     └─ 扫 preferences.image.resolved_path_prefixes
   │         → 按 description fuzzy 匹配
   │         → 自动 image-resolve 有把握的那些
   │
   └── af draft-show --json              （现有；展示 resolved 和 unresolved）

/agentflow-publish <aid>
   └── Step 1b 里多一行：
         Images:      2 resolved / 1 unresolved
         Platforms:
           ghost_wordpress   → 3 images (1 feature + 2 inline)
           linkedin_article  → 1 image (feature only, 2 dropped)
```

## 6. 新 CLI 命令设计

### 6.1 `af propose-images <article_id> [--count N] [--json]`

- 读 draft + metadata + hotspot.source_references
- LLM prompt：给定文章正文，建议 N 个最有价值的插图位（封面 + section 中段 + 必要时 quote 图）
- 输出：带 `[IMAGE:]` 的新 draft.md，以及每个占位的 `description`（精确到能让 D2.5 生成图或让图库搜索）
- 默认 `N = floor(section_count / 2) + 1`，例如 4 section → 3 张
- 写 memory event: `images_proposed`

### 6.2 `af image-auto-resolve <article_id> [--library PATH]`

- 读所有 unresolved placeholders 的 description
- 扫本地图库（默认：`preferences.image.resolved_path_prefixes` 里频率最高的）
- 用 fuzzy / embedding 匹配 description → 文件名 / alt text
- 匹配置信度 ≥ 0.8 才自动绑
- 写 memory event: `image_auto_resolved`
- 失败的仍保留 placeholder，走手动 `af image-resolve`

### 6.3 `af image-generate <article_id> <placeholder_id> [--provider X]`（v0.5+）

- 调 DALL·E / Flux / SD
- 把生成的图落到 `~/.agentflow/drafts/<aid>/images/`
- 自动 image-resolve 到对应 placeholder

## 7. Prompt 设计（`af propose-images`）

关键约束：

1. **不是每段都要图**。文字承载论证，图只在关键节点起作用
2. **避免"装饰性 stock photo"**：描述必须具体到能生成或搜索
3. **封面必要**：第一张图作为 cover，承担社媒分享缩略图角色
4. **quote 图慎用**：Twitter 截图式 quote 图只在 reference 本身是推文时提议

prompt 要求输出 JSON：

```json
{
  "images": [
    {
      "position": "after_opening",            // or "before_section_N" / "middle_of_section_N" / "before_closing"
      "role": "cover | inline | quote",
      "description": "量子纠缠实验装置的示意图，蓝白调，科学论文风",
      "rationale": "这篇主论点是量子纠缠，视觉开场能吸眼球"
    },
    ...
  ]
}
```

然后 CLI 把这些转成 `[IMAGE: description]` 插到对应位置。

## 8. 最小切片（v0.5）

**不做全部**。先交付：

### Slice 1 — `af propose-images`（基础）
- 只实现 LLM 提议插图位 + 写回 draft
- 图源只支持手动（`af image-resolve`）
- 不做 auto-resolve / 不做生成

### Slice 2 — `image_policy` 进 D3 adapters
- Ghost / LinkedIn / Medium 各有 `max_images` 和 `feature_image` 配置
- Step 1b 展示 "该文有 N 张图，Ghost 会用 M 张，LinkedIn 用 K 张"

### Slice 3 — `af image-auto-resolve`
- 扫 preferences.image.resolved_path_prefixes
- 纯文件名 fuzzy 匹配 description（不上 embedding）

Slice 4+（AI 生成、Unsplash、reference 图抓取）放 v0.7+。

## 9. 风险与边界

### 9.1 过度配图
LLM 可能建议每段都插图。prompt 要强约束"优先选择 cover + 2-3 个关键节点 image"。

### 9.2 图源许可
Unsplash/Pexels 免费但要归属声明；AI 生成的商用许可看 provider；抓 reference 原图涉及版权。v0.5 前期只做本地库（用户自担），其他延后。

### 9.3 图片和文本对齐失败
AI 可能在"不适合插图"的地方硬塞（例如数据列表、代码块旁）。prompt 要让 LLM 明确排除这些位置。

### 9.4 平台图片上传机制不一致
- Ghost：HTML 里的 `<img src=...>` 自动转 Ghost CDN 还是直链？需实测
- LinkedIn：API 需要先上传到 LinkedIn assets 获取 URN，再塞到 article
- 当前 adapter 假设 `![desc](path)` 会被各平台正确渲染 —— 可能不对，要在 Slice 2 阶段真打一次实 key

### 9.5 缺图导致发布失败
当前 `--force-strip-images` 可绕过。保留这个安全阀。

## 10. 决策点（需要用户 confirm）

| # | 决策 | 建议 |
|---|---|---|
| I1 | v0.5 只做本地图库 + 手动？ | 是 |
| I2 | `af propose-images` 是 `af write --auto-pick` 的自动后置，还是独立命令？ | 独立 + 默认自动调用；`--skip-images` 可跳 |
| I3 | 封面图 mandatory 吗？ | 建议（非强制）；社媒分享需要 |
| I4 | 图片本地落盘位置？ | `~/.agentflow/drafts/<aid>/images/` |
| I5 | 是否引入 Unsplash API？ | v0.7+，不急 |
| I6 | 是否允许 LLM 生成 alt text？ | 是，`af propose-images` 的 description 就是 alt text |

## 11. 实现成本估计

| 组件 | 估计 |
|---|---|
| `af propose-images` 命令 + prompt | 1 天 |
| D3 adapters 加 image_policy | 0.5 天 |
| Step 1b 补图片信息 | 0.5 天 |
| `af image-auto-resolve` 基础 fuzzy | 0.5 天 |
| skill 层编排 + 测试 | 0.5 天 |
| 文档 | 0.5 天 |

**总计**：3-4 天一个 v0.5 可感知切片（Slice 1 + 2）。Slice 3 再追加 1 天。

## 12. 不做什么

- 不做图片编辑（裁剪/加文字/滤镜）
- 不做版权识别
- 不做 AI 生图的 prompt engineering 精修（只做调用）
- 不自动爬取不属于用户的网络图片
- 不让 AI 选图"替"用户 —— 必须用户拍板
