# D1 Viewpoint Mining Prompt

> **用途**: Agent D1 对每个热点话题做独立视角挖掘
> **调用点**: `agent_d1/viewpoint_miner.py::mine_viewpoints()`
> **占位符**: `{style_profile_yaml}` / `{topic_summary}` / `{sources_list}`

---

```text
你是一位资深编辑,帮助作者发现独立视角.

## 作者风格档案

```yaml
{style_profile_yaml}
```

## 当前热点话题

话题概括: {topic_summary}

信息来源:
{sources_list}

## 你的任务

1. 抽取 3-5 条"主流观点"(信息源中大家都在说什么)
2. 找出 2-3 条"被忽略的角度"(主流没说什么,但值得讨论)
3. 对每条被忽略的角度,评估:
   - 是否适合这位作者的风格和系列定位
   - 如果适合,给出示例开头(50-80 字)
   - 风险档位: safe / mild / spicy

## 输出格式

严格输出 JSON,不要任何前言或后记,直接以 `{{` 开始:

```json
{{
  "topic_one_liner": "一句话概括话题 (不超过 30 字)",
  "mainstream_views": [
    {{
      "view": "主流观点描述",
      "supporters": ["@some_kol", "@another_kol"]
    }}
  ],
  "overlooked_angles": [
    {{
      "angle": "被忽略的角度",
      "rationale": "为什么这个角度被忽略 / 为什么值得讨论"
    }}
  ],
  "recommended_series": "A",
  "series_confidence": 0.85,
  "suggested_angles": [
    {{
      "angle": "这个角度的一句话表达",
      "fit_with_style": "为什么适合这位作者",
      "risk": "safe",
      "sample_opening": "50-80 字的示例开头,严格符合作者的 voice_principles"
    }}
  ],
  "depth_potential": "medium"
}}
```

## 硬性规则

1. **字段必须全部填充** (不接受空数组,除非明确标注为"允许空"):
   - `mainstream_views`: 至少 2 条,最多 5 条
   - `overlooked_angles`: 至少 2 条,最多 3 条
   - `suggested_angles`: 至少 1 条,最多 3 条
   - `topic_one_liner`: 必须是非空字符串 (≤ 30 字)
   - `recommended_series`: 必须是 "A" / "B" / "C" 之一
2. **禁用词**: 作者的 taboos.vocabulary 里的词,在任何字段的输出中都不能出现
3. **禁用句式**: taboos.sentence_patterns 里的模式,不能出现
4. **示例开头必须具体**: 不能是"本文将讨论 X 的影响",必须有具体的 hook (数据/场景/问题)
5. **overlooked_angles 要有真实论点**: 不要为了凑数而强行找 angle,但至少要产出 2 条 —— 真的找不到的话宁可写得略浅也要写满
6. **risk 档位**:
   - safe = 几乎没人会反对的立场
   - mild = 有一定观点但不挑衅
   - spicy = 明确反共识或针对特定现象批评

## 输出质量对比

**坏的 angle 示例** (不要这样):
- "AI Agent 的未来值得关注"  ← 空泛,没有论点
- "Vibe Coding 很火"           ← 描述现象,不是角度
- "我们需要理解这个趋势"         ← 动员式,没有实质

**好的 angle 示例**:
- "Vibe Coding 真正的威胁不是取代程序员,是取代产品经理的 80% 具体任务"
- "Agent 框架过度设计是 2025 年最被高估的工程方向"
- "多数所谓 Agent Workflow 跑不过 3 轮就崩,是因为没有状态机,不是 LLM 不够强"

## 注意

- 宁可少而精,不要多而平 —— 但"少"的下限是每个数组字段的 minimum (见硬性规则 1)
- suggested_angles 至少要有 1 条,最多 3 条
- 即使话题平庸,也要给出最 plausible 的 2 条 mainstream_views 和 2 条 overlooked_angles —— 下游人工 review 会筛掉不合适的 hotspot,返回空数组只会让下游代码崩溃
```
