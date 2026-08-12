# 共享浏览器会话

本项目与事故车提醒共用同一 Playwright Chromium profile 与 CDP 会话（不占用本机 Chrome）。

## 配置

在 `.env` 中设置与事故车相同的路径：

```env
DFMC_DMS_SESSION_HOME=/Users/i/dms-shared-session
DFMC_DMS_BROWSER_EXECUTABLE=/Users/i/Library/Caches/ms-playwright/chromium-*/chrome-mac-*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing
```

目录结构：

```text
$DFMC_DMS_SESSION_HOME/
  .browser-profile/       # Chromium user-data-dir
  .runtime/
    browser-state.json    # CDP port / pid
    keepalive-state.json
    exporting.lock        # 爬虫互斥，保活遇锁跳过刷新
```

## 约定

- 同一时刻只跑一个导出爬虫（`exporting.lock`）
- 本系统默认 **09:00**，事故车 **10:00**，避免冲突
- 登录一次即可：任一控制台「启动登录」后其余插件附着同一会话
