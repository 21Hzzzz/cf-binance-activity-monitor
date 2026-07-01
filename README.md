# Binance Activity Monitor on Cloudflare Workers

Monitor Binance activity entry points on Cloudflare Workers and send new findings to Telegram.

This Worker checks two sources every 15 minutes:

- `marketing/banners`: monitors Binance app homepage banners and sends newly seen linked banners to Telegram.
- `growth-paas/resource/list`: scans resource IDs and sends newly seen `/activity/chance/` activities to Telegram.

The default configuration is sized for the Cloudflare Workers Free plan. Each run uses about 39 subrequests: one KV read, one banner request, up to 35 `resource/list` requests, one Telegram request, and one KV write.

## One-Click Deploy

[![Deploy to Cloudflare Workers](https://deploy.workers.cloudflare.com/button)](https://deploy.workers.cloudflare.com/?url=https://github.com/21Hzzzz/cf-binance-activity-monitor)

Recommended for most users.

1. Click the button above.
2. Authorize Cloudflare to access GitHub and fork this repository when prompted.
3. Keep the Worker name as `cf-binance-activity-monitor` unless you know you need another name.
4. When Cloudflare asks for KV bindings, create or select a KV namespace and bind it with variable name `STATE`.
5. After the first deploy, open the Worker in Cloudflare Dashboard and add runtime variables/secrets.

Required runtime secrets:

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
MONITOR_AUTH_TOKEN
```

Optional runtime secret:

```text
TELEGRAM_MESSAGE_THREAD_ID
```

Optional runtime variables:

```text
LANG = zh-CN
ALERT_ON_FIRST_RUN = false
```

Do not put Telegram tokens into GitHub build variables or into `wrangler.toml`. They should be Worker runtime secrets in Cloudflare Dashboard under **Settings -> Variables and Secrets**.

## GitHub Connected Build

If you connect this repository through **Cloudflare Dashboard -> Workers & Pages -> Create -> Continue with GitHub**, Cloudflare will run:

```text
Build command: bun run typecheck
Deploy command: bun run deploy
Build variables: BUN_VERSION = 1.3.14
```

Before the first connected build can deploy successfully, create a KV namespace and replace the placeholder in `wrangler.toml`:

```toml
[[kv_namespaces]]
binding = "STATE"
id = "REPLACE_WITH_KV_NAMESPACE_ID"
```

The `id` must be the real Cloudflare KV namespace ID. If it stays as `REPLACE_WITH_KV_NAMESPACE_ID`, deployment fails with:

```text
KV namespace 'REPLACE_WITH_KV_NAMESPACE_ID' is not valid
```

After deployment succeeds, add the same runtime secrets listed in the one-click deploy section.

## Local Wrangler Deploy

1. Install dependencies:

```powershell
bun install
```

2. Log in to Cloudflare:

```powershell
bunx wrangler login
```

3. Create a KV namespace:

```powershell
bunx wrangler kv namespace create STATE
```

4. Copy the returned `id` into `wrangler.toml`.

5. Add Telegram secrets:

```powershell
bunx wrangler secret put TELEGRAM_BOT_TOKEN
bunx wrangler secret put TELEGRAM_CHAT_ID
bunx wrangler secret put MONITOR_AUTH_TOKEN
```

Optional Telegram topic/thread:

```powershell
bunx wrangler secret put TELEGRAM_MESSAGE_THREAD_ID
```

6. Deploy:

```powershell
bun run deploy
```

## Manual Verification

After deployment, use:

- `/status`: view latest monitor status.
- `/run?dry=1&token=YOUR_MONITOR_AUTH_TOKEN`: test a run without sending Telegram messages or writing state.
- `/run?token=YOUR_MONITOR_AUTH_TOKEN`: run once, write state, and send newly discovered items.

If `MONITOR_AUTH_TOKEN` is not configured, `/run` is rejected. Scheduled runs are not affected.

## First Run Baseline

By default, `ALERT_ON_FIRST_RUN=false`.

The first run records existing banners and resources without pushing old items to Telegram. Later runs alert only on newly discovered items.

To alert on the first run too, set:

```toml
ALERT_ON_FIRST_RUN = "true"
```
