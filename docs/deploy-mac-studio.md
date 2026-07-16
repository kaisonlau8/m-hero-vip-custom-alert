# Mac Studio 部署说明

## 目标

| 项 | 值 |
|----|-----|
| 目录 | `~/myCode/m-hero-vip-custom-alert` |
| 控制台 | `127.0.0.1:9002` |
| 公网 | https://m-hero-vip-alert.41box.com |
| 监控 | launchd `com.m-hero-vip-custom-alert.watchdog` → 飞书通知刘明轩 |

## 一键步骤（在 Mac Studio）

```bash
cd ~/myCode
git clone https://github.com/kaisonlau8/m-hero-vip-custom-alert.git
# 或已有目录：cd m-hero-vip-custom-alert && git pull

cd ~/myCode/m-hero-vip-custom-alert
python3 scripts/bootstrap.py
cp .env.example .env
# 编辑 .env：APP_SECRET、ADMIN_MOBILE、可选 DFMC_DMS_SESSION_HOME

# Cloudflare Tunnel（新建）
cloudflared tunnel create m-hero-vip-alert
# 记下 tunnel UUID，写入 ~/.cloudflared/config-m-hero-vip-alert.yml
cloudflared tunnel route dns m-hero-vip-alert m-hero-vip-alert.41box.com

# 安装 launchd
cp deploy/com.m-hero-vip-custom-alert.web.plist ~/Library/LaunchAgents/
cp deploy/com.m-hero-vip-custom-alert.watchdog.plist ~/Library/LaunchAgents/
cp deploy/com.cloudflare.cloudflared.m-hero-vip-alert.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.m-hero-vip-custom-alert.web.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.m-hero-vip-custom-alert.watchdog.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.cloudflare.cloudflared.m-hero-vip-alert.plist
```

## 探活

```bash
curl -sS http://127.0.0.1:9002/api/vip/status
curl -sS https://m-hero-vip-alert.41box.com/api/vip/status
launchctl list | grep m-hero-vip
```

## 监控规则

- 每 60s 探测本地 `/api/vip/status`
- 连续失败 ≥2 次 → 飞书文本告警（默认 `WATCHDOG_MOBILE`/`ADMIN_MOBILE`，缺省刘明轩 19272720822）
- 冷却默认 1800s；恢复后发一条恢复通知
