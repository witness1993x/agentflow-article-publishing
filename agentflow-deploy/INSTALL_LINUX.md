# AgentFlow Linux Deploy

> 适用：单用户 Linux VM (Debian/Ubuntu/Arch 都通用)；以非-root user 跑 daemon。

## 0. 前提

- Python 3.11+
- systemd（验证：`systemctl --version`）
- 出口可达 Telegram + 各 platform API（Medium / Dev.to / Hashnode / etc.）
- 已创建非-root 系统用户（示例下文统一用 `agentflow`）

## 1. 同步 source 到 VM

推荐 rsync：保留 `.claude/skills/`，剔除 legacy / build artifacts。

```bash
rsync -avz --delete \
  --exclude '_legacy/' \
  --exclude 'node_modules/' \
  --exclude '__pycache__/' \
  --exclude '.venv/' \
  --exclude '.git/' \
  ./agentflow-article-publishing/ \
  agentflow@vm:/home/agentflow/agentflow-article-publishing/
```

`.claude/skills/` 必须包含；它是 skill harness 安装源。

## 2. Python venv + deps

与 `INSTALL.md` 流程一致：

```bash
cd /home/agentflow/agentflow-article-publishing/backend
python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e .
```

完成后 `af --help` 应可执行。

## 3. Skill harness

```bash
af skill-install            # Claude Code 默认
# 或 Cursor 用户：
af skill-install --cursor
```

## 4. .env 配置

二选一：

- **交互向导**（推荐首次部署）：`af onboard`，按 10 个 sections 逐项填。
- **手填**：复制 `.env.example` 到 `.env`，编辑后跑 `af doctor` 校验。

`af doctor` 全绿（含第 13 项 `daemon_liveness` 暂时 N/A）才进入下一步。

## 5. Profile + Style bootstrap（per-profile 用户）

```bash
# 方式 A：交互向导
af topic-profile init -i --profile <id>

# 方式 B：从 patch 文件
af topic-profile init --profile <id> --from-file <patch.yaml>

# 学风格 + 抽 keyword candidates
af learn-from-handle <handle> --profile <id>
```

每个 profile 都需独立 init；切换 profile 用 `--profile` flag。

## 6. systemd unit 安装

示例 unit 文件：`agentflow-deploy/agentflow-review.service`（同目录）。

```bash
sudo cp agentflow-deploy/agentflow-review.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now agentflow-review.service
systemctl status agentflow-review.service
```

如路径 / user / venv 位置与示例不同，先编辑 `WorkingDirectory` / `ExecStart` / `User` 三项。

## 7. Cron 等价（systemd timer）

```bash
af review-cron-install --times "09:00,18:00"
```

Linux 下应自动生成 systemd timer（`agentflow-review-cron-*.timer`）。

> **TODO**：当前 `review-cron-install` 主线是 macOS launchd；如执行后未在
> `systemctl list-timers` 看到对应 timer，请改用裸 systemd timer 或 cron 兜底。

## 8. 监控

- daemon 心跳：`af doctor` 第 13 项 `daemon_liveness`，读 `~/.agentflow/review/last_heartbeat.json`
- 实时日志：`journalctl -u agentflow-review -f`
- 失败重启：unit 已配 `Restart=on-failure` + `RestartSec=10`
- 状态快照：`systemctl status agentflow-review.service`

## 9. 升级

```bash
# 拉取新代码
cd /home/agentflow/agentflow-article-publishing && git pull
# 或 rsync 新 bundle（步骤 1 命令重跑）

# 重装依赖
cd backend && source .venv/bin/activate && pip install -e .

# 重启 daemon
sudo systemctl restart agentflow-review.service
```

> 偏差告警：`daemon.py` / `render.py` / `triggers.py` 行号会跟版本变；
> 权威行号源：`docs/flows/TG_BOT_FLOWS.md §4`。

## 10. 故障排除

| 症状 | 处置 |
|---|---|
| `preflight failed` | `af doctor`，按红项逐条修 |
| daemon stale | `journalctl -u agentflow-review -n 50` |
| chat_id missing | 在 TG 给 bot 发 `/start`（首次自动捕获） |
| callback 已失效 | 等下一轮，或 `af review-post-{b,c,d} <aid>` 手工补卡 |
| gate D 卡死 | `af review-resume <aid> --to-state image_approved` 强转 |
| import error | 确认 venv 已激活；`pip install -e .` 重装 |

## 安全

见 `SECURITY.md`（同目录）。要点：

- daemon 不以 root 运行
- `.env` 权限 `chmod 600`
- unit 已加 `NoNewPrivileges` / `ProtectSystem=strict` / `ProtectHome=read-only`
- 仅 `~/.agentflow` 可写（见 `ReadWritePaths`）
