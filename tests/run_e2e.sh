#!/usr/bin/env bash
# End-to-end test: throwaway ClickHouse x2 + PostgreSQL containers on 127.0.0.1,
# backup with dbbackup.py, restore into the second ClickHouse, compare.
# Needs Docker, python3, zstd and psql/pg_dump (or Docker fallback).
set -euo pipefail
cd "$(dirname "$0")"
PW='p@ss`w"rd<&>'
IMAGE=clickhouse/clickhouse-server:25.8
cleanup() { docker rm -f dbb-ch-src dbb-ch-dst dbb-pg-src >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup
docker run -d --name dbb-ch-src -e CLICKHOUSE_USER=tester -e CLICKHOUSE_PASSWORD="$PW" \
  -e CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1 -p 127.0.0.1:19900:9000 "$IMAGE" >/dev/null
docker run -d --name dbb-ch-dst -e CLICKHOUSE_USER=tester -e CLICKHOUSE_PASSWORD="$PW" \
  -e CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1 -p 127.0.0.1:19901:9000 "$IMAGE" >/dev/null
docker run -d --name dbb-pg-src -e POSTGRES_PASSWORD=pgtest -p 127.0.0.1:15499:5432 postgres:17 >/dev/null
for c in dbb-ch-src dbb-ch-dst; do
  until docker exec "$c" clickhouse-client --user tester --password "$PW" -q 'SELECT 1' >/dev/null 2>&1; do sleep 1; done
done
until docker exec dbb-pg-src pg_isready -U postgres >/dev/null 2>&1; do sleep 1; done
sleep 2
docker exec -i dbb-ch-src clickhouse-client --user tester --password "$PW" --multiquery < clickhouse_seed.sql
docker exec dbb-pg-src psql -q -U postgres -c 'CREATE DATABASE shop'
docker exec dbb-pg-src psql -q -U postgres -d shop -c 'CREATE TABLE t AS SELECT g AS id, md5(g::text) AS h FROM generate_series(1,50000) g'
python3 test_deps.py
python3 test_e2e.py
