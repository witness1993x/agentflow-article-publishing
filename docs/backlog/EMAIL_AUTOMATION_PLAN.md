# Email 自动化发送流程规划

> Status: 策划中 | Updated: 2026-04-24
> Scope: 把 email 作为 AgentFlow 的第四个发布通道（blog / LinkedIn / Twitter / email）

## 0. Email 的三种用途 — 不能合并设计

| 用途 | 频率 | 收件人 | 模板 | v0.1 做吗 |
|---|---|---|---|---|
| **Newsletter**（订阅式群发） | 周/双周 | 订阅者列表 | 统一模板 | ✓ |
| **1:1 Outreach**（个人 follow-up） | 不定 | 单人 | 个性化 | v0.3 |
| **System Notification**（系统通知） | 触发 | 自己 | 固定 | ✓（简单） |

MVP 覆盖 Newsletter + System Notification。1:1 Outreach 单独做。

---

## 1. Newsletter 是最主要场景 — 从 blog 派生

大多数创作者的 newsletter 就是 "最近一篇博客 + 短编辑导语 + 几条推荐阅读"。
所以 newsletter 不是独立内容生产，是**博客稿的 email 格式化派生**。

### 1.1 派生流程

```
既有 draft <aid>                →  af newsletter-draft <aid>
  ↓                                  ↓
blog 版已发 (optional)           email 版 draft
                                    ↓
                                 af newsletter-send <newsletter_id>
                                    ↓
                                 SMTP / ESP API → 订阅者
```

### 1.2 CLI 命令

```
af newsletter-draft <article_id>            # 从 blog 派生
af newsletter-draft --from-scratch "title"  # 空白起步
af newsletter-show <newsletter_id>
af newsletter-edit <newsletter_id> --section intro --command "加温度"
af newsletter-preview-send <newsletter_id> --to self  # 只发给自己预览
af newsletter-send <newsletter_id> [--list default] [--dry-run]
af newsletter-list-show                     # 订阅者列表状态
af newsletter-list-import <file.csv>        # 导入订阅者
af newsletter-unsubscribe <email>           # 手动退订
```

### 1.3 派生 prompt (`prompts/email_newsletter.md`)

```text
你要把一篇已发博客稿改写成 newsletter 邮件。

【硬性规则】
1. 邮件主题行 ≤ 45 字符（英文）/ 22 字（中文），要钩子
2. 第一句话必须是 "为什么订阅者今天要打开这封邮件" — 不是 "大家好"
3. 正文 3 段：
   - Intro（60-100 字）：你个人的一句话导语
   - Body（引博客稿核心论点 + 一个具体案例 + 链接）
   - Closing（call to action：转发 / 回复 / 订阅）
4. 只允许 inline 图片最多 2 张
5. 底部必须有 {unsubscribe_link} 占位

【输入】
blog draft: {draft_markdown}
published_urls: {published_urls_if_any}
user_handle: {user_handle}

【输出】JSON
{
  "subject": "...",
  "preview_text": "邮件客户端列表里看到的预览 90 字以内",
  "html_body": "...",
  "plain_text_body": "...",
  "images_used": [...]
}
```

### 1.4 落盘

```
~/.agentflow/newsletters/<newsletter_id>/
├── metadata.json
├── content.html
├── content.txt
├── subject.txt
└── images/
```

---

## 2. 发送通道选型

### 2.1 三种选项

| 方案 | 成本 | deliverability | 订阅管理 | 复杂度 |
|---|---|---|---|---|
| A. SMTP via Gmail | 免费（每天 500 封上限） | 中（自发易进垃圾箱） | 自建 | 低 |
| B. Resend / Postmark / SendGrid API | 免费 tier ≈ 100/day | 高（DKIM/SPF 自动）| 自建或用 ESP 的 | 中 |
| C. 第三方 Newsletter SaaS（Substack / Buttondown / ConvertKit） | 免费-付费 | 高 | 内置 | 低（但失去 AgentFlow 集成） |

**推荐 B（Resend）**：
- 免费 100 封/天，10000/月
- API 极简（一个 POST）
- DKIM / SPF / DMARC 自动配
- 自带 audience 管理（可选用，不强制）
- 中国可访问（Postmark / SendGrid 有些 ASN 不稳）

### 2.2 `.env` 新增

```
RESEND_API_KEY=re_...
NEWSLETTER_FROM_EMAIL=you@yourdomain.com
NEWSLETTER_FROM_NAME="Your Name"
NEWSLETTER_REPLY_TO=you@yourdomain.com         # 可与 from 不同
NEWSLETTER_AUDIENCE_ID=aud_...                  # Resend Audience ID
NEWSLETTER_RATE_LIMIT_PER_SEC=10                # 发送节流
```

### 2.3 实现

`backend/agentflow/agent_d4/publishers/email.py` 新文件（对称 Ghost/LinkedIn）：

```python
class EmailPublisher(BasePublisher):
    platform_name = "email_newsletter"
    
    async def publish(self, version):
        # read html + subject + audience
        # POST to https://api.resend.com/emails
        # or loop over audience and POST per recipient (if self-managed list)
        ...
    
    def rollback(self, post_id):
        # Email 已发出去就无法 unsend
        # rollback 含义：给列表发一封更正邮件 (opt-in)
        return False, "email cannot be un-sent; use `af newsletter-correction` to send a correction"
```

---

## 3. 订阅者列表管理

### 3.1 两种策略

**A. 用 Resend Audience**
- 订阅者存在 Resend 侧
- `af newsletter-send` 只需提供 audience_id，Resend 自己循环发
- 退订链接自动由 Resend 处理
- 简单，但订阅者数据在第三方

**B. 自建 list**
- `~/.agentflow/newsletter/subscribers.csv`（email, name, tags, subscribed_at, unsubscribed_at）
- `af newsletter-send` 从 csv 读取，逐个 POST /emails（不用 audience）
- 退订链接指向一个 webhook / 或 mailto 链接，手动处理
- 自主可控但需要处理 bounces 和 unsub

**v0.1 建议 A**（Resend Audience），v0.5 再提供 B 的选项。

### 3.2 订阅入口（第一阶段不做，外部）

订阅者怎么来？
- 手动导入（`af newsletter-list-import`）
- 博客 Ghost 自带的订阅表单 → 用户自己定期导出 CSV 导入
- 单独的 landing page（完全不在 AgentFlow 范围）

---

## 4. Preview / Test send

发给大订阅列表之前，**必须先 test send**：

```
af newsletter-preview-send <newsletter_id> --to self
af newsletter-preview-send <newsletter_id> --to friend@example.com
```

这条命令：
- 不消耗正式 audience
- 不进 publish_history.jsonl
- 只写 `newsletter_preview_sent` memory event
- 检查邮箱客户端渲染（Gmail / Outlook / Apple Mail）

---

## 5. System Notification（自通知）

用 AgentFlow 自己给自己发邮件，用在：

- 定时 hotspots scan 跑完 → 发今日 top 3 hotspot 摘要
- 发布失败 → 发失败详情
- Ghost SSL 重试仍失败 → 发警报

命令：

```
af notify "Hotspots scan complete — 8 clusters, top: XXX"
af notify --event hotspots_scan_complete --template daily_summary
```

走同一个 Resend API，`to` 固定是 `NEWSLETTER_REPLY_TO`（自己）。

---

## 6. 归档 + memory

### 6.1 publish_history.jsonl 扩展

```json
{
  "article_id": "nl_20260424...",
  "platform": "email_newsletter",
  "status": "success | failed | partial",
  "published_url": null,               // email 没有 URL
  "platform_post_id": "resend_email_id",
  "recipient_count": 142,
  "delivered_count": 139,               // 异步回填（Resend webhook）
  "bounced_count": 3,
  "published_at": "...",
  "failure_reason": null
}
```

### 6.2 Memory events

- `newsletter_drafted`
- `newsletter_sent`
- `newsletter_bounced`（Resend webhook 触发）
- `newsletter_unsubscribed`（Resend webhook 触发）

### 6.3 Webhook 处理（v0.5）

Resend 发送后的状态靠 webhook 回写。

- v0.1：不处理 webhook，只记录 "sent at time T"
- v0.5：跑一个本地 `af webhook-server --port 4040`（或扔到 ngrok），Resend 推送 delivered/bounced/opened

---

## 7. 新 skill：`/agentflow-newsletter`

类比 `agentflow-publish`，分 8 步：

1. 确认输入（article_id 或 scratch）
2. 派生 draft（prompt）
3. 预览主题 + 正文 + 预览文字
4. Step 1b pre-send overview：subject 质量、字数、图片、退订链接、audience 大小、deliverability 风险
5. Test send（to self）
6. 确认群发
7. 群发
8. 归档 + 显示 Resend dashboard 链接

---

## 8. 实现顺序（2 周）

### Week 1 — 单通道 MVP
- Day 1: `af newsletter-draft` (prompt + 落盘)
- Day 2: `af newsletter-show` + edit 循环
- Day 3: Resend API 接入 + `af newsletter-preview-send`
- Day 4: `af newsletter-send` 正式发 + rate limit
- Day 5: 落 publish_history + memory event

### Week 2 — 列表 + 通知 + skill
- Day 1: Audience 管理（Resend 侧）+ import CSV
- Day 2: `af notify` 系统通知
- Day 3-4: skill `/agentflow-newsletter` 落地
- Day 5: 真实 key 端到端（test send → 实发一封 newsletter 给自己）

---

## 9. 决策点

| # | 决策 | 建议 |
|---|---|---|
| E1 | ESP 选哪家？ | Resend（免费 tier + 简洁 API） |
| E2 | 列表自建还是用 ESP？ | v0.1 用 Resend Audience；v0.5 支持自建 csv |
| E3 | 是否做 email analytics 回写？ | v0.5（需要 webhook server） |
| E4 | 发送失败是否自动重试？ | 不。bounces 单独处理，其他失败提示用户 |
| E5 | 退订页在哪？ | v0.1 用 Resend 自带；v0.5 自己做（需要 landing） |
| E6 | 是否支持富文本编辑器？ | 否。draft 从 blog 派生，不手动拖拽 |
| E7 | 是否要 send 调度（定时发）？ | v0.3（等 background infra） |

---

## 10. 风险

1. **deliverability**（自己的域名没配 DKIM/SPF/DMARC）→ Resend 引导一次性配好，一天内可验
2. **订阅者很少的时候 send 没意义** → skill 层提醒 "only N subscribers — still send?"
3. **法规（GDPR / CAN-SPAM）**：必须有退订链接 + 发件地址真实；Resend 自动处理
4. **中国用户接收 Gmail 被过滤** → 用 Resend 的自定义域名 + 中文友好 subject，会好很多
5. **Newsletter 发出去就撤不回来** → Step 1b 是唯一防线，必须严格；加 "type 'yes send' 确认" 硬确认

---

## 11. 不做什么

- 不做邮箱 IMAP 收件解析（除非 v0.5 做 1:1 outreach reply parsing）
- 不做 email AB 测试
- 不做邮件模板可视化编辑器
- 不做 email scheduling 系统（v0.1）
- 不做营销漏斗 / drip campaign
- 不提供订阅者 landing page（由用户自己解决）

---

## 12. v0.3 1:1 Outreach 简述

MVP 之外的第二阶段：

```
af outreach-draft --to "name@example.com" --context "article_id or topic"
```

核心差异：
- 不用 audience；每封是独立 1-on-1
- 用 email thread 上下文（如 IMAP 读过的往返历史）
- 更个性化的 prompt（带上对方的 handle / 最近互动）
- 发送走 SMTP via Gmail，而不是 Resend（避免像群发邮件）

留到用户真有 outreach 需求时再细化。
