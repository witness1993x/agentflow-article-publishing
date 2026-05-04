# Feishu / Lark 自建应用权限申请清单

本文件用于 AgentFlow 操作员向所属企业的飞书 / Lark 管理员提交"自建应用"权限申请。
读者：飞书租户管理员（审批人）+ AgentFlow 操作员（提交人）。
目的：让管理员无需追问即可在自建应用后台逐项勾选所需权限。

---

## 1. 目标分层

AgentFlow 当前通过 Telegram 完成 Gate A/B/C/D 人审；本次升级把"同一组人审操作"迁移到飞书群。
为减少审批面，分两个阶段申请：

| 阶段 | 范围 | 状态 |
|---|---|---|
| **Phase 1（必须）** | 在飞书群内完成 Gate A/B/C/D 评审 + 与 Agent 对话追问/修改。机器人收发消息、接收 @、上传/下载附件即可。 | 必须立即批准 |
| **Phase 2（升级，可选）** | 把完整 Markdown 草稿原生化为飞书云文档（docx），替代 v1.0.30 的"截断 + 镜像链接"方案。 | 待 Phase 1 跑通后再申请 |

> Phase 1 是 MVP 的硬门槛；Phase 2 是后续体验升级，可暂缓。

---

## 2. Phase 1 最小权限集

> 飞书近年对 IM 域 scope 做过多轮拆分（旧名 `im:message.send_v1` 已被拆为发送类与接收类）。
> 下表给出当前后台 UI 中应能搜到的 scope code；若管理员只看到旧名，也在备注中给出。

| Scope code | 中文名（后台显示） | 申请理由（1–2 句） | 必需 / 可选 |
|---|---|---|---|
| `im:message` | 获取与发送单聊、群组消息 | 机器人需要主动向群里推送 Gate A/B/C/D 卡片 + 回复操作员追问。等价旧名 `im:message.send_v1`。 | **必需** |
| `im:message.group_at_msg` | 接收群聊中@机器人消息 | 操作员在群里 `@bot` 触发"重写第三段""换标题"等指令的唯一通道。 | **必需** |
| `im:message.group_at_msg:readonly` | 读取群聊中@机器人消息 | 与上一条配套；部分租户后台把读 / 收事件拆成两个开关，两个都勾。 | **必需** |
| `im:chat:readonly` | 获取群组信息 | 启动时按群名自动发现 `chat_id`，避免手填；也用于校验 bot 是否仍在群内。 | 推荐 |
| `im:resource` | 上传与下载图片、文件 | 上传草稿 `.md` / 截图 / 封面图作为消息附件；Phase 1 用作 Gate D 的全文兜底（截断后挂附件）。 | 可选（强烈建议） |
| `contact:user.id:readonly` | 通过手机号或邮箱获取用户 ID | 把 `open_id` 反查回操作员邮箱 / 工号，写入审计日志；不申请也能跑，只是日志里只有 open_id。 | 可选 |

注：飞书 scope 命名历史上出现过 `im:message.send_v1`、`im:message:send_as_bot`、`im:message` 等多种写法，本质是同一能力。后台搜索框输入 `im:message` 通常即可定位。

---

## 3. Phase 1 事件订阅

在"事件订阅"页面（不是权限页）勾选下列事件：

| 事件 code | 用途 |
|---|---|
| `im.message.receive_v1` | 接收用户在群里 @bot 或私聊 bot 发来的文本 / 卡片回复 |
| `card.action.trigger` | 接收 Gate A/B/C/D 交互卡片上的按钮点击（approve / reject / rewrite）|

事件回调对接方需满足：

- **公网 HTTPS** 端点（飞书要求 TLS，自签证书不接受）
- **3 秒内**返回 200 响应；超时即重投，业务必须**幂等**
- 配置了 `encrypt_key` 时，事件 body 是 AES-256-CBC 密文，需先解密
- 每个回调都需用 `verification_token` + 签名头做**签名校验**
- URL 验证阶段需正确回显 `challenge` 字段（明文 JSON）

---

## 4. Phase 2 追加权限（云文档原生草稿）

仅当决定把全文从"附件 / 截断"升级为飞书原生云文档时再申请。
此时可移除 v1.0.30 的"消息体截断 + 镜像链接"逻辑。

| Scope code | 中文名 | 用途 |
|---|---|---|
| `docx:document` | 查看、评论、编辑新版文档 | 创建 / 读取 / 写入 docx 草稿；旧名 `docx:document:readonly` + `docx:document:write` 拆分版亦可 |
| `docx:document:create` | 创建新版文档 | 部分租户把"创建"单独拆出，需要单独勾 |
| `drive:drive` | 查看、编辑和管理云空间中所有文件 | 把 `.md` / `.docx` 上传到指定云空间目录并取回 file_token |
| `drive:file` | 查看、编辑指定云空间文件 | 替代上一条的更小作用域；二选一 |
| `drive:file:upload` | 上传文件到云空间 | 部分租户把上传单独拆出 |

> 升级后 Gate D 卡片直接附"飞书文档链接"，不再贴 markdown 正文，彻底解决长文截断问题。

---

## 5. 授权模型：仅租户级（tenant_access_token）

**Phase 1 与 Phase 2 全部 scope 均可在 `tenant_access_token` 流程下完成，无需任何 `user_access_token` / OAuth 用户授权流程。**

含义：

- 不需要操作员逐人点"同意授权"
- 不需要前端跳转 `https://open.feishu.cn/open-apis/authen/v1/index` 走 OAuth
- AgentFlow 后端只持有 `app_id` + `app_secret`，每 ~2 小时刷新一次 `tenant_access_token` 即可
- 所有"读群信息""发消息""创建文档"动作均以**机器人身份**执行，不以某个具体员工身份执行

这是 scope 选择上最重要的一条边界——**凡是 scope 列表里带 `user_access_token` 字样的都不要勾**。

---

## 6. 操作员提交模板（"权限申请理由"栏直接复制）

### 6.1 单条理由（合并申请，推荐）

```
本应用为内部内容生产流水线 AgentFlow 的飞书侧人审与协作机器人，
负责把 AI 生成的文章草稿推送到指定运营群，由编辑在群内通过卡片按钮
完成 Gate A/B/C/D 四道质量门审核（通过 / 驳回 / 要求改写），
并支持在群里 @机器人 进行追问与局部重写。所有动作均以机器人身份
（tenant_access_token）执行，不读取任何员工个人数据，不涉及用户级
OAuth 授权，仅在固定运营群内收发消息和上传草稿附件。
```

### 6.2 逐项理由（按 scope 拆分填写）

| Scope code | 申请理由（直接粘贴到对应输入框） |
|---|---|
| `im:message` | 机器人需要把 AI 生成的文章草稿和 Gate 评审卡片推送到指定运营群，并回复编辑的追问。 |
| `im:message.group_at_msg` | 编辑在群里通过 @机器人 触发"重写第 N 段""换标题"等指令，这是唯一交互入口。 |
| `im:message.group_at_msg:readonly` | 与上一条配套，读取被 @ 的消息内容（仅在 @ 命中时触发）。 |
| `im:chat:readonly` | 启动时按群名自动发现群 chat_id，避免人工配置出错；也用于检测 bot 是否仍在群内。 |
| `im:resource` | 把 markdown 草稿、封面图、截图作为附件上传到群里，供编辑下载查看。 |
| `contact:user.id:readonly` | 把审核操作的 open_id 反查为员工邮箱写入审计日志，便于事后追溯具体由谁通过 / 驳回。 |
| `docx:document`（Phase 2） | 把完整草稿写入飞书云文档，替代当前"消息截断 + 外链"方案，编辑可直接在飞书内批注。 |
| `drive:drive` 或 `drive:file`（Phase 2） | 上传 .md / .docx 到指定云空间目录并取回 file_token，供 Gate D 卡片挂链接。 |

---

## 7. 网络与回调端点要求

| 项 | 要求 |
|---|---|
| 协议 | HTTPS only（TLS 1.2+），飞书不接受 HTTP / 自签证书 |
| 端点形态 | 单一 POST URL，例如 `https://<your-domain>/lark/events` |
| 响应时间 | **3 秒内**必须返回 2xx，否则视为失败并重投 |
| 幂等性 | URL 验证 challenge 必须幂等；业务事件按 `event_id` 去重，重投不可触发二次发卡 |
| Body 大小 | 单事件 body 上限约 1 MB；附件下载走单独 API，不走事件体 |
| 鉴权 | 校验 `X-Lark-Signature` + `X-Lark-Request-Timestamp`，时间戳偏移 > 5 分钟拒绝 |
| 加密 | 若后台设置了 `encrypt_key`，事件 body 为 AES-256-CBC base64 密文，必须解密后再处理 |
| IP 白名单 | 飞书出口 IP 段官方有公布；如本侧 WAF 有 allowlist 需求请向网络组同步 |

---

## 8. 凭据与安全要求

四个秘密都由飞书后台生成，**绝不进 Git**：

| 名称 | 用途 | 存放位置 |
|---|---|---|
| `app_id` | 应用唯一标识（半公开，但仍按机密管理） | `~/.agentflow/secrets/.env` 或同目录 `.env.lark` |
| `app_secret` | 换取 tenant_access_token | 同上 |
| `verification_token` | 事件回调签名校验 | 同上 |
| `encrypt_key` | 事件回调 AES 解密（若启用） | 同上 |

落盘要求：

- 文件权限 `chmod 0600`，目录 `chmod 0700`
- 仅运行 AgentFlow 服务的系统用户可读
- `.gitignore` 必须覆盖 `~/.agentflow/secrets/` 与仓库内任何 `.env*`
- 轮换：`app_secret` / `encrypt_key` 至少每 90 天轮换一次；轮换后旧值立即失效

---

## 9. 明确不申请（Out of scope）

为降低管理员审批负担，以下能力**本次不申请**，操作员遇到管理员追问时可直接答复"不需要"：

- **任何 `user_access_token` 用户级 scope** —— 全部走机器人身份，不读员工个人数据
- **通讯录全量读取**（如 `contact:contact`、`contact:department.base:readonly`）—— 仅按 user_id 反查邮箱，不爬通讯录
- **日历类**（`calendar:calendar`、`calendar:calendar.event`）
- **审批 / 流程类**（`approval:*`）
- **视频会议 / VC**（`vc:meeting`、`vc:room`）
- **邮箱 / 邮件**（`mail:*`）
- **OKR / 绩效**（`okr:*`、`performance:*`）
- **跨租户、开放平台分销商、ISV 商城类** scope —— 自建应用场景完全不涉及

如管理员看到上述任何一项被勾选，请取消——可能是后台默认带选。

---

## 附：审批通过后操作员需回填给开发者的信息

| 字段 | 来源 | 用途 |
|---|---|---|
| `app_id` | 应用后台"凭证与基础信息" | 服务启动 |
| `app_secret` | 同上（点"重置"后只显示一次，注意保存） | 服务启动 |
| `verification_token` | "事件订阅"页 | 回调校验 |
| `encrypt_key` | "事件订阅"页（若启用加密） | 回调解密 |
| 事件回调 URL | 由开发者提供，操作员填回后台 | 飞书向我方推送事件 |
| 目标群 chat_id 或群名 | 操作员在群里 `/get_chat_id` 或由 bot 自动发现 | 服务定向推送 |
| 机器人 open_id | 后台"应用功能 → 机器人" | 群内识别自身 @ |

文件权限审批走完后，请把上述字段以**安全渠道**（密码管理器 / 加密邮件）发给 AgentFlow 维护者，**不要贴在 IM 聊天里**。
