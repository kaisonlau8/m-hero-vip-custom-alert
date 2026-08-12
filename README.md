# VIP 客户保养提醒

每日监控 DMS 保养提醒任务，命中 VIP 客户清单后，通过飞书应用 **HeroClaw** 通知多维表格中登记的提醒人；督导消息带「已提醒门店」闭环按钮，跟踪写入独立多维表「VIP 客户维保跟踪」。

> 工具集总览 / 文档地图 / 依赖关系：[m-hero](https://github.com/kaisonlau8/m-hero)

| 项 | 值 |
|----|-----|
| 本地控制台 | `http://127.0.0.1:9002` |
| 卡片回调 | `POST /feishu/card`（公网经 cloudflared 指向 `:9002`，hostname 不入库） |
| 黄页（本地） | `http://127.0.0.1:9004` |
| 共享会话 | 与事故车、区域报表共用 DMS Chromium（[shared-browser-session.md](docs/shared-browser-session.md)） |

## 能力

| 时间 | 动作 |
|------|------|
| **00:00** | 同步飞书多维表：VIP 清单 + 提醒人（含「提醒人角色」） |
| **09:00** | DMS 导出保养提醒 → VIN 匹配 → 按角色发卡片；督导任务 upsert「VIP 客户维保跟踪」（**同一任务编码只提醒一次**） |

### 角色与卡片

| 提醒人角色 | 卡片 | 跟踪表 |
|------------|------|--------|
| 督导 | 带「已提醒门店」按钮 | 按 `(任务编码, 督导)` upsert；点击后异步回写状态与确认时间 |
| 管理员 / 未填 | 纯通知、无按钮 | 不写 |

登记底稿复用当日 DMS 导出 + VIP 缓存；已存在或已闭环的行只补全业务字段，**不重置**跟踪状态 / 确认时间 / 消息 ID，也不重复发卡。

同人多行提醒人配置会合并「区域」「提醒级别」；任一行为督导则按督导处理。

## 快速开始

```bash
python3 scripts/bootstrap.py
cp .env.example .env
# 填写 APP_SECRET、TRACKING_*、FEISHU_VERIFICATION_TOKEN（及可选 FEISHU_ENCRYPT_KEY）
# 部署 Mac Studio 时设置 DFMC_DMS_SESSION_HOME

./run.sh --console
```

控制台中：

1. **启动登录** → 在浏览器完成 DMS 登录  
2. （可选）**开始录制** → 走一遍保养提醒页导出 → **停止录制**  
3. **同步多维表** → **爬取+匹配发送** 或用案例 Excel dry-run  

CLI：

```bash
# 仅同步
./run.sh --sync

# 用案例 Excel 干跑（不发消息）
./run.sh --pipeline --skip-crawl \
  --import-xlsx download/保养提醒任务列表_20260812_092738.xlsx --dry-run

# 测试发送到 ADMIN_MOBILE（测试账号按督导发带按钮卡）
./run.sh --test --skip-crawl \
  --import-xlsx download/保养提醒任务列表_20260812_092738.xlsx

# 正式：保活 + 00:00/09:00 调度
./run.sh --prod
```

## 多维表格

### VIP / 提醒人（知识库 Base）

- Base token：`LaF7bGsZ5aGxIbskmP7cnp1Qnac`（`BITABLE_APP_TOKEN`）
- VIP：`tblO0YW2AG2lPJBn`（M817 VIP清单）— VIN / 姓名 / 客户类别 / VIP级别 / VIP属性 / 车系  
- 提醒人：`tblBCgluJyPS8NWT`（[VIP 超级提醒](https://m-hero.feishu.cn/wiki/WILYwiyINiHiz5kOEvMc3T0enRh?table=tblBCgluJyPS8NWT&view=vewDU6ILnO)）  
  — 提醒人（联系人）/ 区域 / 提醒级别 / **提醒人角色**（`管理员`｜`督导`）  
- 路由：DMS「区域」∩ VIP「VIP级别」精确匹配提醒人的「区域」「提醒级别」

### 闭环跟踪「VIP 客户维保跟踪」

- Base：[YKtMbDFwjaoOSosJ8j4cBT74nbe](https://m-hero.feishu.cn/base/YKtMbDFwjaoOSosJ8j4cBT74nbe)（`TRACKING_BITABLE_APP_TOKEN`）  
- 表 ID：`TRACKING_TABLE_ID`（示例 `tblnvsSaaEzRaXQZ`）  
- 写入凭证：`TRACKING_APP_ID` / `TRACKING_APP_SECRET`（须开通 `bitable:app`）；未授权时 `TRACKING_FALLBACK_TO_MAIN=1` 回退 HeroClaw  

| 字段分组 | 字段 | 更新策略 |
|----------|------|----------|
| DMS 任务 | 任务编码、VIN、门店编码/名称、区域、任务类型/状态、售后车系、用车人/车主及电话、下次预约时间、预约单号、任务创建日期、到期日期、首次回访日期/人、DMS有效状态、关闭时间 | 每日 upsert 刷新 |
| VIP | 客户姓名、VIP级别/属性、客户类别、车系 | 每日 upsert 刷新 |
| 系统 | 最近同步时间、提醒触发时间、督导、飞书消息ID | 同步时间每日刷新；触发时间/消息 ID 仅首次写入 |
| 闭环 | 跟踪状态（待提醒门店 / 已提醒门店）、督导确认时间、确认人 | 仅按钮回写；upsert **不覆盖** |

## 卡片回传配置（HeroClaw）

回调须在 **3 秒内** HTTP 200 响应（超时客户端报 `200341`）。本服务先返回 toast + 更新后的卡片，多维表回写与消息 PATCH 异步执行。

1. [飞书开放平台](https://open.feishu.cn/) → 应用 **HeroClaw**  
2. **事件与回调** → 添加 **卡片回传交互**（建议只保留新版 `card.action.trigger`，不要新旧两套并存）  
3. 请求地址：`https://<your-hostname>/feishu/card`（对应本机 `127.0.0.1:9002`）  
4. 将 Verification Token 写入 `.env` 的 `FEISHU_VERIFICATION_TOKEN`  
5. 若开启了 Encrypt Key，同步写入 `FEISHU_ENCRYPT_KEY`（需依赖 `pycryptodome`）  
6. 发布应用版本；改 `.env` 后执行：  
   `launchctl kickstart -k gui/$(id -u)/com.m-hero-vip-custom-alert.web`  
7. 提醒人表：督导填「提醒人角色=督导」，总部只读填「管理员」  
8. 自检：

```bash
curl -s http://127.0.0.1:9002/feishu/card
# {"ok":true,"service":"vip-alert-card-callback"}

curl -s https://<your-hostname>/feishu/card
```

## 关键脚本

| 路径 | 说明 |
|------|------|
| `scripts/bitable_sync.py` | 同步 VIP + 提醒人（含角色合并） |
| `scripts/import_excel.py` | 解析 DMS 保养提醒 Excel 全量业务列 |
| `scripts/match_and_notify.py` | VIN 匹配、角色分卡、督导 upsert、IM 去重 |
| `scripts/feishu_client.py` | HeroClaw 发卡 / 更新卡 |
| `scripts/tracking_bitable.py` | 跟踪表 create/upsert/回写 |
| `scripts/web_console.py` | `:9002` 控制台 + `/feishu/card` 回调 |
| `scripts/crawl_maintenance_reminder.py` | DMS 导出爬虫 |
| `scripts/pipeline.py` / `scheduler.py` | 流水线与定时 |

## 数据文件

| 路径 | 说明 |
|------|------|
| `download/*.xlsx` | DMS 导出 |
| `data/vip_cache.json` | VIP VIN 缓存 |
| `data/recipients_list.json` | 提醒人列表（含 `role`） |
| `data/sent_tasks.json` | 已发送任务编码（IM 去重） |
| `config/recipients.json` | 手机号 → open_id 缓存 |
| `recordings/` | 录制会话 |

## 环境变量（摘要）

见 [`.env.example`](.env.example)：

- HeroClaw：`APP_ID` / `APP_SECRET`  
- VIP/提醒人表：`BITABLE_*`  
- 跟踪表：`TRACKING_APP_*` / `TRACKING_BITABLE_APP_TOKEN` / `TRACKING_TABLE_ID` / `TRACKING_FALLBACK_TO_MAIN`  
- 卡片回调：`FEISHU_VERIFICATION_TOKEN` / `FEISHU_ENCRYPT_KEY`  
- 测试：`ADMIN_MOBILE`  
- 共享浏览器：`DFMC_DMS_SESSION_HOME` / `DFMC_DMS_BROWSER_EXECUTABLE`  

**勿提交** `.env`、密钥、含手机号的联系人表。

## 部署（Mac Studio）

详见 [docs/deploy-mac-studio.md](docs/deploy-mac-studio.md)。

1. 控制台端口 **9002**（公网 hostname 仅写本机 cloudflared，不入库）  
2. 与事故车 / 区域报表共用 `DFMC_DMS_SESSION_HOME`；时刻表条目 `vip-alert` = **09:00**（提前 3 分钟禁强刷，见 [docs/shared-browser-session.md](docs/shared-browser-session.md)）  
3. launchd 托管控制台 + Cloudflare Tunnel + 挂死监控（飞书通知）  
4. Tunnel 必须指向控制台，以便 `/feishu/card` 可被飞书访问  

## 时区锁定（UTC+8 北京）

调度、日志、文件名时间戳一律使用 `Asia/Shanghai`：

- `scripts/time_utils.py`：`ensure_beijing_tz()` + `beijing_*` 工具  
- `run.sh` / `.env`：`TZ=Asia/Shanghai`  
- launchd plist：`EnvironmentVariables.TZ=Asia/Shanghai`  

## 爬虫说明

基线脚本：`scripts/crawl_maintenance_reminder.py`  
路由：`#/aftermarketMange/customerManagement/maintenanceReminderTask`  

若页面按钮选择器与基线不一致，请用控制台录制器操作一遍，再根据 `recordings/*/events.jsonl` 微调选择器。
