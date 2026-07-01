# Binance Activity Monitor on Cloudflare Workers

这是一个部署在 Cloudflare Workers 上的 Binance 活动监控器，会定时检查 Binance 活动入口，并把新发现推送到 Telegram。

Worker 默认每 15 分钟监控两个来源：

- `marketing/banners`：监控 Binance App 首页 banner，发现新的带链接 banner 后推送到 Telegram。
- `growth-paas/resource/list`：按 resource id 扫描，发现新的 `/activity/chance/` 活动后推送到 Telegram。

默认配置按 Cloudflare Workers Free 计划控制请求量。每轮大约使用 39 个 subrequests：1 次 KV 读取、1 次 banner 请求、最多 35 次 `resource/list` 请求、1 次 Telegram 请求、1 次 KV 写入。

## 一键部署

[![Deploy to Cloudflare Workers](https://deploy.workers.cloudflare.com/button)](https://deploy.workers.cloudflare.com/?url=https://github.com/21Hzzzz/cf-binance-activity-monitor)

推荐大多数用户使用这个方式。

1. 点击上方按钮。
2. 按 Cloudflare 提示授权 GitHub，并 fork 这个仓库。
3. Worker 名称建议保持为 `cf-binance-activity-monitor`。
4. Cloudflare 提示配置 KV 时，创建或选择一个 KV namespace，并把绑定变量名设为 `STATE`。
5. 首次部署完成后，进入 Cloudflare Dashboard 的 Worker 设置页，添加运行时变量和密钥。

必填运行时密钥：

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
MONITOR_AUTH_TOKEN
```

可选运行时密钥：

```text
TELEGRAM_MESSAGE_THREAD_ID
```

可选运行时变量：

```text
LANG = zh-CN
ALERT_ON_FIRST_RUN = false
```

不要把 Telegram token 写进 GitHub build variables，也不要写进 `wrangler.toml`。这些值应该放在 Cloudflare Worker 的运行时 **Variables and Secrets** 里。

## GitHub 自动构建部署

如果你通过 **Cloudflare Dashboard -> Workers & Pages -> Create -> Continue with GitHub** 连接这个仓库，推荐配置：

```text
Build command: bun run typecheck
Deploy command: bun run deploy
Build variables: BUN_VERSION = 1.3.14
```

第一次部署前必须先创建 KV namespace，并把 `wrangler.toml` 里的占位符替换成真实 KV namespace id：

```toml
[[kv_namespaces]]
binding = "STATE"
id = "REPLACE_WITH_KV_NAMESPACE_ID"
```

`id` 必须是真实的 Cloudflare KV namespace id。如果保留占位符，部署会失败：

```text
KV namespace 'REPLACE_WITH_KV_NAMESPACE_ID' is not valid
```

部署成功后，再添加“一键部署”部分列出的运行时密钥。

## 本地 Wrangler 部署

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

4. 把命令输出里的 `id` 填入 `wrangler.toml`。

5. 添加 Telegram 密钥：

```powershell
bunx wrangler secret put TELEGRAM_BOT_TOKEN
bunx wrangler secret put TELEGRAM_CHAT_ID
bunx wrangler secret put MONITOR_AUTH_TOKEN
```

如果 Telegram 群组启用了 topic/thread，可以额外添加：

```powershell
bunx wrangler secret put TELEGRAM_MESSAGE_THREAD_ID
```

6. 部署：

```powershell
bun run deploy
```

## 手动验证

部署完成后可以访问：

- `/status`：查看最近一次监控状态。
- `/run?dry=1&token=YOUR_MONITOR_AUTH_TOKEN`：试跑一次，不发送 Telegram，也不写入状态。
- `/run?token=YOUR_MONITOR_AUTH_TOKEN`：手动运行一次，写入状态，并推送新增发现。

如果没有配置 `MONITOR_AUTH_TOKEN`，`/run` 会拒绝访问。定时任务不受影响。

## 首轮基线

默认 `ALERT_ON_FIRST_RUN=false`。

第一次运行只记录当前已经存在的 banner 和 resource，不推送旧内容。后续运行发现新增内容时才会推送。

如果你希望第一次运行也推送扫描到的内容，可以设置：

```toml
ALERT_ON_FIRST_RUN = "true"
```
