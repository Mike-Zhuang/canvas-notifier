#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
# 专用本地测试实例，仅监听 loopback；不修改系统已有 PostgreSQL 服务。
mkdir -p .local
if [ ! -f .local/pgdata/PG_VERSION ]; then
  initdb -D .local/pgdata -U canvas_notifier --auth-local=trust --auth-host=trust > .local/pg-init.log
fi
if ! pg_ctl -D .local/pgdata status >/dev/null 2>&1; then
  pg_ctl -D .local/pgdata -l .local/postgres.log -o '-p 55432 -h 127.0.0.1 -k /tmp' start
fi
if ! psql -h 127.0.0.1 -p 55432 -U canvas_notifier -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='canvas_notifier'" | rg -q 1; then
  createdb -h 127.0.0.1 -p 55432 -U canvas_notifier canvas_notifier
fi
