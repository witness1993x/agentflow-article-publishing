# D2 Skeleton Generation Prompt

> **用途**: Agent D2 基于选定的 hotspot 和 angle,生成文章骨架候选
> **调用点**: `agent_d2/skeleton_generator.py::generate_skeleton()`

---

```text
你是作者的写作搭档.

## 作者风格档案

```yaml
{style_profile_yaml}
```

{topic_intent_block}

{publisher_account_block}

## 目标系列

{target_series} 系列: {series_description}

系列偏好:
- 典型字数: {typical_length}
- 主要语言: {primary_language}
- 开头风格: {opening_style}
- 结尾风格: {closing_style}
- 典型小节: {typical_sections}

## 选定的热点话题

话题: {topic}
切入角度: {chosen_angle}
角度说明: {angle_fit_explanation}

## 你的任务

生成该文章的骨架候选,包括:

### 1. 3 个标题候选

- 第 1 个: 陈述式 (给出结论/立场)
- 第 2 个: 疑问式 (提出有引力的问题)
- 第 3 个: 叙事式 (用一个场景/故事片段做标题)

每个标题 20-40 字符,不要空话套话.

### 2. 3 个开头候选

每个 50-80 字,严格符合 opening_style "{opening_style}".

- 第 1 个: 数据/事实式 (具体数字 / 时间 / 金额 / 场景)
- 第 2 个: 故事式 (用第一人称经历开头)
- 第 3 个: 问题式 (抛出一个让读者停下来的问题)

注意:开头决定留存率,必须有 hook.

### 3. 分节大纲 (4-6 个小节)

每个小节包含:
- heading: 小节标题 (15-30 字)
- key_arguments: 2-4 条具体论点 (每条一句话,不要空话)
- estimated_words: 预估字数 (总和应约等于目标字数 {target_length})
- section_purpose: 这节要让读者理解什么

### 4. 3 个结尾候选

每个 50-100 字,严格符合 closing_style "{closing_style}".

- 第 1 个: 行动号召式 (给读者具体可执行的下一步)
- 第 2 个: 开放问题式 (抛出一个值得读者思考的问题)
- 第 3 个: 升华式 (把话题升到更大 context)

## 输出格式

严格输出 JSON:

```json
{{
  "title_candidates": [
    {{"title": "...", "style": "declarative", "rationale": "为什么这个标题"}},
    {{"title": "...", "style": "question", "rationale": "..."}},
    {{"title": "...", "style": "narrative", "rationale": "..."}}
  ],
  "opening_candidates": [
    {{"opening_text": "...", "style": "data", "hook_strength": "strong", "rationale": "..."}},
    {{"opening_text": "...", "style": "story", "hook_strength": "medium", "rationale": "..."}},
    {{"opening_text": "...", "style": "question", "hook_strength": "strong", "rationale": "..."}}
  ],
  "section_outline": [
    {{
      "heading": "小节标题",
      "key_arguments": ["论点 1", "论点 2", "论点 3"],
      "estimated_words": 500,
      "section_purpose": "这节要达成的阅读目标"
    }}
  ],
  "closing_candidates": [
    {{"closing_text": "...", "style": "cta", "rationale": "..."}},
    {{"closing_text": "...", "style": "open", "rationale": "..."}},
    {{"closing_text": "...", "style": "elevation", "rationale": "..."}}
  ]
}}
```

## 硬性规则

1. **禁用词**: taboos.vocabulary 里的词绝对不能出现
2. **禁用句式**: taboos.sentence_patterns 里的模式不能出现
3. **标题不重复**: 3 个标题必须角度明显不同,不是同一句话换个说法
4. **开头不重复**: 3 个开头必须风格明显不同
5. **大纲论点具体**: 不要 "讨论 X 的影响" 这种抽象描述,必须有具体论点
6. **字数总和校验**: section_outline 的 estimated_words 总和应约等于 {target_length}

## 质量对比

**坏的标题示例**:
- "关于 AI Agent 的思考"  ← 空
- "AI Agent 的未来发展趋势"  ← 空
- "我对 AI Agent 的一些看法"  ← 弱化

**好的标题示例**:
- "为什么 AI Agent 框架都跑不过 3 轮"
- "Vibe Coding 不会取代程序员,只会让 PM 变得没用"
- "我拿 Claude Code 重写了一周的工作,发现的 5 个反常识"

**坏的 key_argument 示例**:
- "讨论 Agent 的发展"
- "分析各种技术路线"

**好的 key_argument 示例**:
- "主流 Agent 框架都假设 LLM 能维持长程上下文,但实测超过 20 轮就漂"
- "Vibe Coding 的真正价值不是让程序员写代码更快,是让 PM 不需要再给程序员讲需求"
```
