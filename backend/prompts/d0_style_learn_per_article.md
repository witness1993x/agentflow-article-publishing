# D0 Style Learn — Per-Article Prompt

> **用途**: 对每篇样稿单独分析,抽取该篇的风格特征
> **调用点**: `agent_d0/per_article_analyzer.py::analyze_article()`
> **占位符**: `{article_text}` / `{article_title_hint}`

---

```text
你是风格分析助手,帮作者从一篇样稿里提取写作风格特征.

作者会提供一篇过往文章,你的任务是把这篇的风格量化成一个 JSON 对象,
供后续聚合生成整体 style_profile 使用.

## 文章标题(可能为空)

{article_title_hint}

## 文章正文

```
{article_text}
```

## 你的任务

仔细阅读上面的文章,逐项抽取风格特征,严格按照下面的 JSON schema 输出.

所有字段都必须出现,不要省略任何一项.不能判断时给保守默认值
(例如 tone_intensity 不确定就写 "medium").

## JSON Schema

```json
{{
  "language": "zh" | "en" | "bilingual",
  "zh_ratio": 0.0,
  "avg_para_words": 0,
  "max_para_words": 0,
  "min_para_words": 0,
  "avg_sentence_words": 0,
  "voice_principles": [
    {{
      "key": "短标识,如 direct_no_preamble",
      "description": "这条原则的一句话说明(可用中文或英文,与文章主语言一致)"
    }}
  ],
  "signature_phrases": [
    "作者反复出现的口头禅或标志短语,3-8 条"
  ],
  "taboo_candidates": [
    "作者几乎从不使用的、常见但 AI 味浓的词,3-10 条"
  ],
  "tone_intensity": "gentle" | "medium" | "medium_sharp" | "sharp" | "very_sharp",
  "structural_pattern": "描述文章的大骨架,如 hook_problem_analysis_conclusion",
  "emoji_density": "low" | "medium" | "high"
}}
```

## 字段说明

1. **language**: 判断主导语言. 中英混合且中文占比 ≥ 70% 标 zh,
   ≤ 30% 标 en, 其余标 bilingual.
2. **zh_ratio**: 中文字符数 / 总有效字符数, 范围 0.0-1.0.
3. **avg/max/min_para_words**: 按空行分段,统计词数
   (中文字符按 1 词, 英文空格分词).
4. **avg_sentence_words**: 按中英文标点切句后的平均词数.
5. **voice_principles**: 最多 5 条, 必须从这篇文章里能找到证据.
   - 好的 key 示例: `show_dont_tell`, `honest_failure`, `practice_over_theory`
   - 坏的 key 示例: `good_writing` (太空), `clear` (太泛)
6. **signature_phrases**: 作者反复用的口头禅,带语气的短语,
   例如 "说白了", "本质上", "I used to think...".
7. **taboo_candidates**: 这篇文章中**从未出现**且 AI 典型爱用的词.
   常见 AI 词池参考: "赋能", "闭环", "赛道", "颠覆", "绝对",
   "综上所述", "值得注意的是", "首先其次最后".
   只纳入这篇文章确实没用过的,否则留空.
8. **tone_intensity**:
   - gentle = 克制保守
   - medium = 中性
   - medium_sharp = 有观点但不挑衅
   - sharp = 有锋芒
   - very_sharp = 直接批评现象
9. **structural_pattern**: 用下划线短语描述文章整体骨架.
10. **emoji_density**: 文章中 emoji 每 500 词出现次数.
    < 1 = low, 1-5 = medium, > 5 = high.

## 硬性规则

- 只输出 JSON,不要任何前言、解释或后记.
- 所有数字字段必须是数字,不要带引号.
- 如果某个列表找不到内容,返回空数组 [],不要返回 null.
- 不要编造 signature_phrases,必须真的在文章里出现.

## 输出

直接以 `{{` 开始输出 JSON.
```
