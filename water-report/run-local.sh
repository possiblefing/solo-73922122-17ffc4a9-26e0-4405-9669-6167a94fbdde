#!/usr/bin/env bash
# 本机无 Docker 时的开发/演示启动脚本（容器环境请用 docker compose up）。
# 用法: ./run-local.sh
set -euo pipefail
cd "$(dirname "$0")"

VENV="${VENV:-.venv}"
PY="$PWD/$VENV/bin/python"
LEGACY_PORT="${LEGACY_PORT:-8011}"
REPORTER_PORT="${REPORTER_PORT:-8010}"
export REPORTER_DB="${REPORTER_DB:-$PWD/data/reporter.db}"
export UPLOAD_DIR="${UPLOAD_DIR:-$PWD/data/uploads}"
export SCREENSHOT_DIR="${SCREENSHOT_DIR:-$PWD/data/screenshots}"
export LEGACY_DB="${LEGACY_DB:-$PWD/data/legacy.db}"
export LEGACY_URL="http://127.0.0.1:${LEGACY_PORT}"
export HEADLESS="${HEADLESS:-1}"
export BROWSER_CHANNEL="${BROWSER_CHANNEL:-chromium}"   # 本机只有完整 Chromium
export SLOW_MO_MS="${SLOW_MO_MS:-150}"

# 本机 Chromium 依赖库解压在用户目录时，需要补 LD_LIBRARY_PATH
if [ -d "$HOME/.local/chromelibs" ]; then
  export LD_LIBRARY_PATH="$(find "$HOME/.local/chromelibs/usr/lib" "$HOME/.local/chromelibs/lib" \
      -type d 2>/dev/null | tr '\n' ':')${LD_LIBRARY_PATH:-}"
fi

mkdir -p data/uploads data/screenshots
"$PY" -m uvicorn legacy.app:app --host 127.0.0.1 --port "$LEGACY_PORT" \
  > data/legacy.log 2>&1 &
LEGACY_PID=$!
echo "legacy station pid=$LEGACY_PID -> http://127.0.0.1:${LEGACY_PORT}"
"$PY" -m uvicorn reporter.app:app --host 127.0.0.1 --port "$REPORTER_PORT" \
  > data/reporter.log 2>&1 &
REPORTER_PID=$!
echo "reporter console pid=$REPORTER_PID -> http://127.0.0.1:${REPORTER_PORT}"
echo "$LEGACY_PID" > data/legacy.pid
echo "$REPORTER_PID" > data/reporter.pid
trap 'kill $LEGACY_PID $REPORTER_PID 2>/dev/null || true' INT TERM
wait
