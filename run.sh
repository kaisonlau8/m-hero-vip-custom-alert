#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$ROOT/.venv/bin/python"

# 强制北京时间，避免跟随机器本地时区
export TZ=Asia/Shanghai

if [[ ! -x "$PYTHON" ]]; then
  echo "虚拟环境不存在，先运行: python3 $ROOT/scripts/bootstrap.py" >&2
  exit 1
fi

# shellcheck disable=SC1091
if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$ROOT/.env"
  set +a
fi

MODE="help"
SKIP_CRAWL=""
TEST_PHONE="${ADMIN_MOBILE:-}"
CONSOLE=""
CONSOLE_PORT="${CONSOLE_PORT:-9002}"
CONSOLE_HOST="${CONSOLE_HOST:-127.0.0.1}"
IMPORT_XLSX=""
DRY_RUN=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --test) MODE="test"; shift ;;
    --prod) MODE="prod"; shift ;;
    --sync) MODE="sync"; shift ;;
    --morning) MODE="morning"; shift ;;
    --pipeline) MODE="pipeline"; shift ;;
    --skip-crawl) SKIP_CRAWL="--skip-crawl"; shift ;;
    --import-xlsx) IMPORT_XLSX="$2"; shift 2 ;;
    --test-phone) TEST_PHONE="$2"; shift 2 ;;
    --dry-run) DRY_RUN="--dry-run"; shift ;;
    --console) CONSOLE="yes"; shift ;;
    --port) CONSOLE_PORT="$2"; shift 2 ;;
    --host) CONSOLE_HOST="$2"; shift 2 ;;
    -h|--help)
      cat <<'EOF'
用法: ./run.sh [选项]

  --console           启动 Web 控制台（默认端口 9002）
  --sync              仅同步飞书多维表（VIP + 提醒人）
  --pipeline          执行完整流水线（爬取→匹配→发送）
  --morning           等同于 --pipeline（09:00 任务）
  --test              测试模式：消息发给 --test-phone / ADMIN_MOBILE
  --prod              正式模式：保活 + 00:00/09:00 定时调度
  --skip-crawl        跳过爬取，使用 --import-xlsx 或 download/ 最新文件
  --import-xlsx PATH  指定导入的保养提醒 Excel
  --dry-run           只匹配不发送
  --test-phone N      测试收件人手机号
  --port N / --host A 控制台监听

示例：
  python3 scripts/bootstrap.py
  ./run.sh --console
  ./run.sh --sync
  ./run.sh --pipeline --skip-crawl --import-xlsx download/保养提醒任务列表20260714104313.xlsx --dry-run
  ./run.sh --test --skip-crawl --import-xlsx download/xxx.xlsx
  ./run.sh --prod
EOF
      exit 0
      ;;
    *) echo "未知参数: $1" >&2; exit 1 ;;
  esac
done

DRY_RUN="${DRY_RUN:-}"

if [[ "$CONSOLE" == "yes" ]]; then
  echo "VIP 保养提醒 — Web 控制台  http://${CONSOLE_HOST}:${CONSOLE_PORT}"
  exec "$PYTHON" "$ROOT/scripts/web_console.py" --host "$CONSOLE_HOST" --port "$CONSOLE_PORT"
fi

case "$MODE" in
  sync)
    exec "$PYTHON" "$ROOT/scripts/bitable_sync.py"
    ;;
  pipeline|morning)
    ARGS=(--morning)
    [[ -n "$SKIP_CRAWL" ]] && ARGS+=(--skip-crawl)
    [[ -n "$IMPORT_XLSX" ]] && ARGS+=(--import-xlsx "$IMPORT_XLSX")
    [[ -n "$DRY_RUN" ]] && ARGS+=(--dry-run)
    exec "$PYTHON" "$ROOT/scripts/scheduler.py" "${ARGS[@]}"
    ;;
  test)
    ARGS=(--test --test-phone "$TEST_PHONE")
    [[ -n "$SKIP_CRAWL" ]] && ARGS+=(--skip-crawl)
    [[ -n "$IMPORT_XLSX" ]] && ARGS+=(--import-xlsx "$IMPORT_XLSX")
    [[ -n "$DRY_RUN" ]] && ARGS+=(--dry-run)
    exec "$PYTHON" "$ROOT/scripts/scheduler.py" "${ARGS[@]}"
    ;;
  prod)
    echo "VIP 保养提醒 — 正式模式（保活 + 定时调度）"
    "$PYTHON" "$ROOT/scripts/keepalive_browser.py" &
    KEEPALIVE_PID=$!
    trap "kill $KEEPALIVE_PID 2>/dev/null; exit 0" SIGINT SIGTERM
    exec "$PYTHON" "$ROOT/scripts/scheduler.py"
    ;;
  *)
    echo "请指定模式，见 ./run.sh --help" >&2
    exit 1
    ;;
esac
