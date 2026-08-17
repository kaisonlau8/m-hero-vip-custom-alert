# 共享浏览器会话

本项目与 **事故车提醒**、**区域报表自动化** 共用同一 Playwright Chromium profile 与 CDP 会话（不占用本机 Chrome）。

权威说明：[m-hero/docs/SHARED_DMS_BROWSER.md](https://github.com/kaisonlau8/m-hero/blob/main/docs/SHARED_DMS_BROWSER.md)

## 配置

在 `.env` 中设置：

```env
DFMC_DMS_SESSION_HOME=/Users/i/dms-shared-session
DFMC_DMS_BROWSER_EXECUTABLE=/path/to/Google Chrome for Testing
```

```text
$DFMC_DMS_SESSION_HOME/
  .browser-profile/
  .runtime/
    browser-state.json
    keepalive-state.json
    keepalive.log
    exporting.lock
    crawl_schedule.json
    crawl_registry.json
```

## 爬取时刻表（与本项目相关）

共享文件：`$DFMC_DMS_SESSION_HOME/.runtime/crawl_schedule.json`

| id | 计划时间 | 本仓任务 | owner |
|----|----------|----------|--------|
| `vip-alert` | **09:00** | 导出保养提醒 → 匹配发送 | `vip_maintenance_reminder` |

错峰邻居：区域报表 `08:30`，事故车 `10:00` / `17:00`。

保护规则：

- **09:00 前 3 分钟**起禁刷，直至本爬取登记结束
- 到点未开跑时，最长禁刷至约 `09:00 + 45min`
- `acquire_export_lock(..., schedule_id="vip-alert")` 时自动登记

改 09:00 调度时，同步改时刻表里 `vip-alert.time`。

## 约定

- 同一时刻只跑一个导出爬虫（`exporting.lock`）
- 任一控制台完成 DMS 登录后，其余插件附着同一会话
- 冷启动带 `--disable-extensions` 与 `--use-mock-keychain`，避免每次输 macOS 密码；换参数后可能要重登一次 DMS
