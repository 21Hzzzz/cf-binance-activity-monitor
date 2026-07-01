# Binance Activity Monitor on Cloudflare Workers

这个 Worker 每 15 分钟监控两个来源：

- `marketing/banners`：App 首页 banner，发现新的带链接 banner 推送 Telegram。
- `growth-paas/resource/list`：按 resource id 扫描，发现新的 `/activity/chance/` 活动推送 Telegram。

默认按 Cloudflare Workers Free 计划控制预算：每轮最多 35 次 `resource/list` 请求，加上 KV、banner、Telegram，总 subrequests 约 39 次，低于 Free 的 50/request 限制。

## 部署步骤

### 方式 A：Cloudflare Dashboard 连接 GitHub

可以把这个目录作为一个单独 GitHub repository 上传，然后在 Cloudflare 的 **Create Worker -> Continue with GitHub** 里选择它。Cloudflare 官方支持把 GitHub repo 连接到 Worker，并在每次 push 后自动部署。

推荐设置：

```text
Root directory: 留空
Build command: bun run typecheck
Deploy command: bun run deploy
Build variables: BUN_VERSION = 1.3.14
```

如果你上传的是整个 `rebn` 目录，而不是单独上传本项目目录，则设置：

```text
Root directory: cf-binance-activity-monitor
Build command: bun run typecheck
Deploy command: bun run deploy
Build variables: BUN_VERSION = 1.3.14
```

GitHub 自动部署前仍然需要先处理 KV：

1. 在 Cloudflare Dashboard 创建 KV namespace，例如 `binance_activity_monitor_state`。
2. 复制 KV namespace ID。
3. 把 `wrangler.toml` 里的 `REPLACE_WITH_KV_NAMESPACE_ID` 改成真实 ID。
4. 提交并 push 到 GitHub。

首次部署成功后，在 Worker 的 **Settings -> Variables and Secrets** 添加 runtime secrets：

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
MONITOR_AUTH_TOKEN
```

可选：

```text
TELEGRAM_MESSAGE_THREAD_ID
```

注意：Cloudflare 的 Build Variables and Secrets 只给构建过程使用，不等于 Worker 运行时变量。Telegram token 和 chat id 要放在 Worker 的 runtime **Variables and Secrets** 里。

### 方式 B：本机 Wrangler 部署

1. 安装依赖：

```powershell
bun install
```

2. 登录 Cloudflare：

```powershell
bunx wrangler login
```

3. 创建 KV namespace：

```powershell
bunx wrangler kv namespace create STATE
```

把输出里的 `id` 填入 `wrangler.toml` 的 `[[kv_namespaces]]`。

4. 写入 Telegram secrets：

```powershell
bunx wrangler secret put TELEGRAM_BOT_TOKEN
bunx wrangler secret put TELEGRAM_CHAT_ID
```

如果群组开启了 topic，可以额外设置：

```powershell
bunx wrangler secret put TELEGRAM_MESSAGE_THREAD_ID
```

5. 可选：设置手动触发 token：

```powershell
bunx wrangler secret put MONITOR_AUTH_TOKEN
```

6. 部署：

```powershell
bun run deploy
```

## 手动验证

部署后可以访问：

- `/status`：查看最近状态。
- `/run?dry=1&token=你的MONITOR_AUTH_TOKEN`：试跑，不发 Telegram，不写入状态。
- `/run?token=你的MONITOR_AUTH_TOKEN`：手动跑一次，会写状态并推送新增发现。

如果没有设置 `MONITOR_AUTH_TOKEN`，`/run` 会拒绝访问，定时任务不受影响。

## 首轮基线

默认 `ALERT_ON_FIRST_RUN=false`。第一次运行只记录当前已经存在的链接，不推送旧活动。之后发现新增链接才推送。

如果你想第一次运行也把扫描到的链接推到 Telegram，把 `wrangler.toml` 改成：

```toml
ALERT_ON_FIRST_RUN = "true"
```
