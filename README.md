# Binance Activity Monitor

这是一个可部署在 Debian 服务器上的轻量 Binance 活动监控脚本。它会定时检查 Binance 活动入口，并把新发现推送到 Telegram。

当前监控两个来源：

- `marketing/banners`：监控 Binance App 首页 banner，发现新的带链接 banner 后推送到 Telegram。
- `growth-paas/resource/list`：按 resource id 扫描，发现新的 `/activity/chance/` 活动后推送到 Telegram。

脚本使用 Python 标准库实现，不需要第三方 Python 依赖。安装脚本会创建 systemd service 和 timer，默认每 15 分钟运行一次。

## 一键安装

在 Debian 服务器上执行：

```bash
curl -fsSL https://raw.githubusercontent.com/21Hzzzz/cf-binance-activity-monitor/main/install.sh -o /tmp/cf-binance-activity-monitor-install.sh
sudo bash /tmp/cf-binance-activity-monitor-install.sh
```

安装过程会提示填写：

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
TELEGRAM_MESSAGE_THREAD_ID 可选
```

非交互安装也可以这样传参：

```bash
curl -fsSL https://raw.githubusercontent.com/21Hzzzz/cf-binance-activity-monitor/main/install.sh -o /tmp/cf-binance-activity-monitor-install.sh
sudo env TELEGRAM_BOT_TOKEN="你的 bot token" TELEGRAM_CHAT_ID="你的 chat id" bash /tmp/cf-binance-activity-monitor-install.sh
```

安装完成后会立即运行一次基线检查。默认 `ALERT_ON_FIRST_RUN=false`，所以第一次运行只记录已有内容，不会推送旧活动。

## 一键卸载

保留配置和状态数据：

```bash
sudo /opt/cf-binance-activity-monitor/uninstall.sh
```

同时删除配置和状态数据：

```bash
sudo /opt/cf-binance-activity-monitor/uninstall.sh --purge
```

如果本地脚本已经被删，也可以直接下载卸载脚本：

```bash
curl -fsSL https://raw.githubusercontent.com/21Hzzzz/cf-binance-activity-monitor/main/uninstall.sh -o /tmp/cf-binance-activity-monitor-uninstall.sh
sudo bash /tmp/cf-binance-activity-monitor-uninstall.sh --purge
```

## 文件位置

```text
/opt/cf-binance-activity-monitor/monitor.py       主程序
/etc/cf-binance-activity-monitor.env             运行配置
/var/lib/cf-binance-activity-monitor/state.json  去重和扫描状态
/etc/systemd/system/cf-binance-activity-monitor.service
/etc/systemd/system/cf-binance-activity-monitor.timer
```

配置文件权限会设置为 `600`，用于保存 Telegram token。

## 常用命令

查看定时器：

```bash
sudo systemctl status cf-binance-activity-monitor.timer
```

手动运行一次：

```bash
sudo systemctl start cf-binance-activity-monitor.service
```

查看日志：

```bash
sudo journalctl -u cf-binance-activity-monitor.service -n 100 --no-pager
```

试跑一次，不发送 Telegram，也不写入状态：

```bash
sudo python3 /opt/cf-binance-activity-monitor/monitor.py --env-file /etc/cf-binance-activity-monitor.env run --dry-run
```

查看当前状态：

```bash
sudo python3 /opt/cf-binance-activity-monitor/monitor.py --env-file /etc/cf-binance-activity-monitor.env status
```

## 配置项

主要配置在 `/etc/cf-binance-activity-monitor.env`：

```text
TELEGRAM_BOT_TOKEN=你的 bot token
TELEGRAM_CHAT_ID=你的 chat id
TELEGRAM_MESSAGE_THREAD_ID=可选 topic/thread id
BINANCE_LANG=zh-CN
ALERT_ON_FIRST_RUN=false
RESOURCE_BATCH_SIZE=100
RESOURCE_BATCHES_PER_RUN=35
RESOURCE_PROBE_BATCHES_PER_RUN=10
RESOURCE_SCAN_START_ID=100003800
RESOURCE_BACKTRACK=800
RESOURCE_RECENT_AHEAD=1700
ERROR_ALERT_COOLDOWN_SECONDS=3600
```

修改配置后可以直接手动运行一次 service，下一轮 timer 也会读取新配置。

## 首轮基线

默认：

```text
ALERT_ON_FIRST_RUN=false
```

第一次运行只记录当前已经存在的 banner 和 resource，不推送旧内容。后续运行发现新增内容时才会推送。

如果你希望第一次运行也推送扫描到的内容，可以改成：

```text
ALERT_ON_FIRST_RUN=true
```

## 错误提醒

如果 Binance 接口、网络或 Telegram 推送出现错误，脚本会把错误发送到 Telegram。相同错误默认 1 小时冷却一次，避免刷屏：

```text
ERROR_ALERT_COOLDOWN_SECONDS=3600
```
