# 图片素材端到端实施方案（Implementation Plan）

> Status: 策划中 | Updated: 2026-04-24 | Depends: IMAGE_INSERTION_STRATEGY.md 抽象框架
> Scope: 从"抽象策略"落到"可编码的 3 周工作"

本文档是 `IMAGE_INSERTION_STRATEGY.md` 的**实施版**。策略回答"为什么这么做"，本文件回答"具体怎么写代码"。

## 0. 三阶段能力拆分

| 阶段 | 做的事 | 新命令 | 依赖 |
|---|---|---|---|
| **A. 图片需求提取** | 让 LLM 读完整 draft 后，产出"这里要插什么图"的结构化清单 | `af propose-images` | D2 已出 draft |
| **B. 图源阅读理解** | 扫本地图库 + reference 自带图 + 可选生图；让 LLM 判断"这张图配不配得上这个需求" | `af image-auto-resolve` | A 已出需求清单 |
| **C. 位置植入 + 平台排版** | 把图正式嵌进 draft；D3 按平台 image_policy 裁剪/排版 | 已有 `af image-resolve` + D3 adapter 扩展 | A + B 之一 |

v0.5 目标：A + C 最小可用；B 做到"扫本地图库自动匹配"。

---

## A. 图片需求提取 —— `af propose-images <article_id>`

### A.1 输入

```
draft.md                          ← 当前文章
metadata.json                     ← target_series
hotspot source_references[]       ← 原始信号（有时自带图）
style_profile.image_preferences   ← 用户偏好（新字段，见 A.5）
```

### A.2 Prompt 设计（backend/prompts/d2_image_proposal.md）

骨架：

```text
你是{user_handle}的视觉编辑助理。给你一篇已完成的 Markdown 文章 + 3-5 条信息源，
判断哪些位置需要配图，输出结构化 JSON。

【硬性约束】
1. **不是每段都要图**。以下位置才考虑：
   - 开头后 (cover，几乎必放)
   - 一个论证转折点
   - 一个数据/案例节点
   - 结尾前 (可选)
2. 图片数量：section_count / 2 + 1，± 1
3. 避开以下位置：代码块、bullet list 中间、引用 quote 块内
4. 每张图必须有具体可搜/可生成的 description，禁止 "abstract illustration"

【输出 JSON schema】
{
  "images": [
    {
      "position": {
        "anchor": "after_opening | before_section:N | middle_of_section:N | before_closing",
        "rationale": "为什么插这里"
      },
      "role": "cover | inline | quote_screenshot | diagram",
      "description": {
        "en": "英文 alt text / 搜索关键词",
        "zh": "中文 alt text"
      },
      "style_hint": "photograph | illustration | screenshot | chart | 示意图",
      "source_hint": "local_library | generate | reference_quote | stock_api",
      "priority": "required | recommended | optional"
    }
  ]
}

【draft】
{draft_markdown}

【references】
{source_references_top5}
```

### A.3 位置语义（anchor 解析）

| anchor | 在 draft.md 中的实际落点 |
|---|---|
| `after_opening` | opening 段末尾空行后 |
| `before_section:N` | `## N-th heading` 之前空行 |
| `middle_of_section:N` | N-th section 的中位段落之后 |
| `before_closing` | closing 段之前空行 |

插入格式固定为 `[IMAGE: <description.zh>]`（一行，独占一段）。这样复用既有 `markdown_utils._IMAGE_PLACEHOLDER_RE`。

### A.4 落盘 + memory

- 写回 `draft.md`（新版本）
- 同时写 `~/.agentflow/drafts/<aid>/image_proposals.json` 保留原始 LLM 输出（包含 rationale、role、style_hint，不进 draft.md）
- 写 `images_proposed` memory event：`{article_id, count, total_sections, required_count, recommended_count}`

### A.5 Style profile 新字段

```yaml
# style_profile.yaml 新增
image_preferences:
  default_style: photograph | illustration | mixed
  feature_image_required: true
  max_images_per_article: 5
  preferred_aspect: 16:9 | 4:3 | square
  caption_policy: auto | manual | none
  # 用户可以手动加，D0 learn-style 后期可从样本文章自动推
```

---

## B. 图源阅读理解 —— `af image-auto-resolve <article_id>`

### B.1 四类图源优先级

```
1. reference 原图       （Twitter 推文里的图 / RSS 文章 og:image）
2. 本地图库             （默认 ~/Pictures/agentflow/）
3. AI 生成              （Flux / SD / DALL·E，v0.7+）
4. 公共图源             （Unsplash / Pexels，v0.7+）
```

v0.5 只做 1 + 2。3/4 延后。

### B.2 Reference 图抓取

每条 `source_reference` 已经有 `url`。对每条：

- 若是 Twitter URL：调 Twitter API v2 `/tweets/{id}?expansions=attachments.media_keys&media.fields=url` 拿到图 URL
- 若是普通网页 URL：`httpx.get(url)` + `<meta property="og:image">` 解析
- 把拿到的图 URL 下载到 `~/.agentflow/drafts/<aid>/images/ref_<N>.jpg`

**前置**：需要 `httpx` + `beautifulsoup4`（已在 requirements 里核验）。

### B.3 本地图库扫描

```python
# 伪码
for placeholder in unresolved_placeholders:
    query = placeholder.description
    candidates = []
    for f in walk(IMAGE_LIB_PATH):
        if f.ext in {".png",".jpg",".jpeg",".webp"}:
            score = match_score(
                filename = basename(f),
                exif_desc = exif(f).get("ImageDescription") or "",
                query = query,
            )
            if score > 0:
                candidates.append((score, f))
    candidates.sort(reverse=True)
    if candidates and candidates[0][0] >= 0.7:
        auto_resolve(placeholder, candidates[0][1])
    else:
        keep_unresolved(placeholder)
```

`match_score` v0.5 简单实现：
- 关键词 token 化（jieba 或简单 split）
- 文件名 + EXIF 的关键词与 query token 的 Jaccard
- 加成：query 中 5+ 字节的短语在文件名 substring 命中 → +0.3

v0.7 可升级：用 Jina v3 embed description + 文件名，余弦相似度。

### B.4 LLM 二次确认（可选 `--strict`）

`--strict` 模式下，match_score 通过后，再把图的文件名 + EXIF + query description 传给 Kimi，让它输出 `{match: true/false, reason: "..."}`。防止字面命中但语义不配。

### B.5 结果

- 成功匹配 → 直接调 `image-resolve`
- 未匹配 → placeholder 保留，写 `--unresolved-report.json`
- 写 `images_auto_resolved` memory event：`{article_id, resolved: N, still_unresolved: M}`

---

## C. 位置植入 + 平台排版

### C.1 单文章层（`af image-resolve` 已有）

无改动。字符串替换 `[IMAGE: desc]` → `![desc](path)`。

### C.2 D3 adapter 扩展：`image_policy`

新增到 `backend/agentflow/agent_d3/platform_rules.py`：

```python
"ghost_wordpress": {
    ...
    "image_policy": {
        "max_images": 10,
        "preferred_image_count": [3, 5],
        "feature_image": "first",        # 第一张作 feature_image
        "inline_image_mode": "native",   # Ghost 支持原生 <img>
        "caption_from_alt": True,
    },
},
"linkedin_article": {
    ...
    "image_policy": {
        "max_images": 5,
        "preferred_image_count": [1, 2],
        "feature_image": "first",
        "inline_image_mode": "native",
        "caption_from_alt": False,
        "drop_images_beyond_max": True,  # 超过 max 从尾部丢
    },
},
"twitter_thread": {
    "image_policy": {
        "max_images": 4,              # Twitter 单推 4 图上限
        "preferred_image_count": [1, 2],
        "feature_image": "first",
        "per_tweet_max": 4,
        "inline_image_mode": "split_across_thread",
    },
},
"email_newsletter": {
    "image_policy": {
        "max_images": 5,
        "inline_image_mode": "cid_attachment | public_cdn_url",
        "feature_image": "first",
        "force_max_width_px": 600,
    },
},
```

### C.3 Ghost adapter 具体改动

`backend/agentflow/agent_d3/adapters/ghost.py`：

```python
def adapt(self, draft):
    # existing paragraph/heading logic...
    images = extract_resolved_images(draft)  # 新 helper
    if images:
        feature = images[0]
        meta["feature_image"] = feature.path  # → Ghost 的 feature_image
        # 保留 inline <img> 不变
```

Ghost publisher 里，feature_image 要先**上传到 Ghost Storage API** 换 CDN URL，不能直接塞本地路径。新增 `agent_d4/publishers/ghost.py::_upload_image(local_path) -> cdn_url`。

### C.4 LinkedIn adapter

LinkedIn Article API 不支持 inline 多图直接塞 markdown。需要：
1. 每张图先调 `POST /rest/images?action=initializeUpload` 获取 upload URL
2. PUT 图到 upload URL
3. 拿到 image URN（`urn:li:image:xxx`）
4. 在 article body 的对应位置塞 `<img>` 指向 URN

这是重活。v0.5 先简化：**LinkedIn 只上传 feature_image（第一张），内联图全部丢弃**。用户在 Step 1b 能看到 "LinkedIn: 1 image (feature only, N-1 dropped)"。

### C.5 Email 与 Twitter 的 image 适配

见各自独立的 SOP / Plan 文档。

---

## D. 整合进 agentflow-write skill

`.claude/skills/agentflow-write/SKILL.md` 在 Step 4（resolve images）之前插 **Step 3.5**：

```
## Step 3.5 — propose images (new, optional but default-on)

After the edit loop ends, ask: "Run image proposal? (yes — recommended / skip)"

If yes:
    PYTHONPATH=. af propose-images <article_id> --json

Show the user:
    [1] after_opening   cover   "量子纠缠实验装置示意图"  (priority: required)
    [2] middle_section:1 inline "Stanford CS101 课堂照片"   (priority: recommended)
    ...

Then ask: "Accept all / reject (idx) / regenerate all"

After acceptance, draft.md gets [IMAGE:] placeholders. Proceed to Step 4
(image-resolve) with the normal flow — or try auto-resolve first:

    PYTHONPATH=. af image-auto-resolve <article_id> --library ~/Pictures/agentflow --json

Report "N auto-resolved, M still manual".
```

---

## E. Step 1b（发布前）图片维度

`agentflow-publish/SKILL.md` 的 Step 1b 追加一行：

```
Images:   <R> resolved / <U> unresolved
  Ghost:    <N> will be embedded (feature: <path>, inline: <N-1>)
  LinkedIn: <M> will be embedded (feature only; <N-M> dropped)
  Twitter:  <K> will be embedded (across thread of T tweets)
```

Hallucination flag 现在也覆盖图片：**如果图的 description 和 refs 里的图描述 / 文章论点都对不上**，flag "image_drift"（很可能是生图时幻觉了一张不存在的场景）。

---

## F. 实现顺序（3 周）

### Week 1 — A 阶段
- Day 1-2: prompt + `af propose-images` 命令
- Day 3: anchor 解析 + draft.md 改写
- Day 4: skill 集成 + memory event
- Day 5: 真实文章测试 + 调 prompt

### Week 2 — B 阶段
- Day 1: reference 图抓取（Twitter media API + og:image）
- Day 2-3: 本地图库扫描 + match_score v0.5
- Day 4: `af image-auto-resolve` 命令
- Day 5: `--strict` LLM 二次确认

### Week 3 — C 阶段 + 整合
- Day 1: D3 adapter image_policy 落地
- Day 2: Ghost publisher 图上传
- Day 3: LinkedIn feature_image only
- Day 4: Step 1b 图片维度 + hallucination flag
- Day 5: 端到端真实 key smoke

---

## G. 决策点（需要用户 confirm）

| # | 决策 | 建议 |
|---|---|---|
| IP1 | 默认图库路径？ | `~/Pictures/agentflow/`（用户可改 `.env: AGENTFLOW_IMAGE_LIBRARY`） |
| IP2 | `propose-images` 默认自动跑还是手动触发？ | 自动（skill 层默认 on），`--skip-images` 可关 |
| IP3 | feature_image 强制吗？ | 强推不强制。无图时 skill 提示但允许跳过 |
| IP4 | v0.5 是否做 AI 生图？ | 否，留给 v0.7 |
| IP5 | LinkedIn 只上传首图是否可接受？ | 是（v0.5 简化），v0.7 做全量 multi-image |
| IP6 | Twitter thread 的图片分配策略？ | 首推放 cover，其余 thread 推按 anchor 顺序分配，每推最多 1 图 |

---

## H. 风险

1. **Kimi 可能建议不合理位置**（例如 bullet list 中间插图）→ 硬约束写死 prompt + 后处理校验
2. **图库匹配假阳性**（文件名含关键词但图不相关）→ `--strict` 兜底
3. **Ghost upload API 限速** → 每张图之间 sleep 0.5s
4. **LinkedIn image URN 复杂** → v0.5 只做 feature，延后 inline
5. **Twitter thread 的图需要按推拆分** → 只在 Twitter adapter 里处理，不污染别的平台

## I. 成本

- Kimi 读一次 draft 挖图位：~3000 input + 500 output tokens ≈ $0.001/次
- reference 抓图：免费（用户自己的 Twitter token + 直接 HTTP）
- 本地图库扫描：零外部成本
- 实施时间：3 周（单人）

## J. 不做什么

- 不做图片编辑（裁剪/滤镜/加水印）
- 不做版权/许可自动识别
- 不爬取不属于用户的网络图
- 不做 AI 生图的超参调优
- 不给图加自动生成的文字标注（meme-style）
