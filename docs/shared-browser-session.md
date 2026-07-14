# 共享浏览器会话

本项目与事故车提醒、投诉爬虫共用同一 Chrome profile 与 CDP 会话。

## 配置

在 `.env` 中设置与其它插件相同的路径：

```env
DFMC_DMS_SESSION_HOME=/path/to/dms-shared-session
```

目录结构：

```text
$DFMC_DMS_SESSION_HOME/
  .browser-profile/       # Chrome user-data-dir
  .runtime/
    browser-state.json    # CDP port / pid
    keepalive-state.json
    exporting.lock        # 爬虫互斥，保活遇锁跳过刷新
```

## 约定

- 同一时刻只跑一个导出爬虫（`exporting.lock`）
- 本系统默认 **09:00**，事故车 **10:00**，避免冲突
- 登录一次即可：任一控制台「启动登录」后其余插件附着同一会话
- 详细说明见 `dfmc-dms-crawler/docs/shared-browser-session.md`
