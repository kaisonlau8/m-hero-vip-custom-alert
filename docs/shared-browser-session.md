# 共享浏览器会话

本项目与 **事故车提醒**、**区域报表自动化** 共用同一 Playwright Chromium profile 与 CDP 会话（不占用本机 Chrome）。

权威说明：[m-hero/docs/SHARED_DMS_BROWSER.md](https://github.com/kaisonlau8/m-hero/blob/main/docs/SHARED_DMS_BROWSER.md)

## 配置

在 `.env` 中设置：

```env
DFMC_DMS_SESSION_HOME=/Users/i/dms-shared-session
DFMC_DMS_BROWSER_EXECUTABLE=/Users/i/Library/Caches/ms-playwright/chromium-1217/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing
```

```text
$DFMC_DMS_SESSION_HOME/
  .browser-profile/
  .runtime/
    browser-state.json
    keepalive-state.json
    exporting.lock
    crawl_schedule.json
    crawl_registry.json
```

## 约定

- 本系统默认 **09:00** 爬取；区域报表 08:30、事故车 10:00 / 17:00
- 开跑前 3 分钟至登记完成期间不会被保活强刷
- 同一时刻只跑一个导出爬虫（`exporting.lock`）
- 任一控制台完成 DMS 登录后，其余插件附着同一会话
