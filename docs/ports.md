# 端口约定（VIP 保养提醒）

| 端口 | 服务 | 说明 |
|------|------|------|
| **9000** | accident-vehicle-reminder | 事故车提醒，勿占用 |
| **9001** | （预留） | 历史投诉爬虫等，勿随意占用 |
| **9002** | **m-hero-vip-custom-alert** | 本项目 Web 控制台（默认） |
| 动态 | Chrome CDP | 写入 `DFMC_DMS_SESSION_HOME/.runtime/browser-state.json` |

公网经 Cloudflare Tunnel 反代到 `:9002`；hostname 仅写本机 cloudflared，不入库。

检查：

```bash
lsof -nP -iTCP:9000,9001,9002 -sTCP:LISTEN
```

启动：

```bash
./run.sh --console
# → http://127.0.0.1:9002
```
