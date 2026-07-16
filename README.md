# VIP 客户保养提醒

每日监控 DMS 保养提醒任务，命中 VIP 客户清单后，通过飞书应用 **HeroClaw** 通知多维表格中登记的提醒人。

## 能力

| 时间 | 动作 |
|------|------|
| **00:00** | 同步飞书多维表：VIP 清单 + 提醒人 |
| **09:00** | DMS 全量导出保养提醒 → VIN 匹配 → 飞书卡片（**同一任务编码只提醒一次**） |

控制台默认：`http://127.0.0.1:9002`

## 快速开始

```bash
python3 scripts/bootstrap.py
cp .env.example .env   # 已提供时可直接改 .env
# 填写 APP_SECRET；部署 Mac Studio 时设置 DFMC_DMS_SESSION_HOME

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
  --import-xlsx download/保养提醒任务列表20260714104313.xlsx --dry-run

# 测试发送到 ADMIN_MOBILE
./run.sh --test --skip-crawl \
  --import-xlsx download/保养提醒任务列表20260714104313.xlsx

# 正式：保活 + 00:00/09:00 调度
./run.sh --prod
```

## 多维表格

- VIP：`tblO0YW2AG2lPJBn`（M817 VIP清单）— VIN / 姓名 / 客户类别 / VIP级别 / VIP属性 / 车系  
- 导出补充：门店名称、区域、任务类型、创建日期、任务编码  
- 提醒人：`tblBCgluJyPS8NWT`（[VIP 超级提醒](https://m-hero.feishu.cn/wiki/WILYwiyINiHiz5kOEvMc3T0enRh?table=tblBCgluJyPS8NWT&view=vewDU6ILnO)）— 提醒人（飞书联系人）/ 区域 / 提醒级别  
- 路由规则：DMS 任务「区域」∩ VIP「VIP级别」精确匹配提醒人的「区域」「提醒级别」  
- Base token：`LaF7bGsZ5aGxIbskmP7cnp1Qnac`

## 数据文件

| 路径 | 说明 |
|------|------|
| `download/*.xlsx` | DMS 导出 |
| `data/vip_cache.json` | VIP VIN 缓存 |
| `data/recipients_list.json` | 提醒人列表 |
| `data/sent_tasks.json` | 已发送任务编码（去重） |
| `config/recipients.json` | 手机号 → open_id 缓存 |
| `recordings/` | 录制会话 |

## 部署（Mac Studio）

1. 与事故车插件设置相同的 `DFMC_DMS_SESSION_HOME`  
2. 控制台端口 **9002**（见 [docs/ports.md](docs/ports.md)）  
3. 共享浏览器说明见 [docs/shared-browser-session.md](docs/shared-browser-session.md）  
4. 启动：`./run.sh --prod` 或 `./run.sh --console` 后点「启动定时等候」

## 爬虫说明

基线脚本：`scripts/crawl_maintenance_reminder.py`  
路由：`#/aftermarketMange/customerManagement/maintenanceReminderTask`  

若页面按钮选择器与基线不一致，请用控制台录制器操作一遍，再根据 `recordings/*/events.jsonl` 微调选择器。
