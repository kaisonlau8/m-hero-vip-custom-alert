# 端口约定（VIP 保养提醒）

| 端口 | 服务 | 说明 |
|------|------|------|
| **9000** | accident-vehicle-reminder | 事故车提醒，勿占用 |
| **9001** | dfmc-dms-crawler | 投诉爬虫控制台，勿占用 |
| **9002** | **m-hero-vip-custom-alert** | 本项目 Web 控制台（默认） |
| 公网 | https://m-hero-vip-alert.41box.com | Cloudflare Tunnel → 9002 |
| 动态 | Chrome CDP | 写入 `DFMC_DMS_SESSION_HOME/.runtime/browser-state.json` |

检查：

```bash
lsof -nP -iTCP:9000,9001,9002 -sTCP:LISTEN
```

启动：

```bash
./run.sh --console
# → http://127.0.0.1:9002
```
