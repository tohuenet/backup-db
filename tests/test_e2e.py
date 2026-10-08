#!/usr/bin/env python3
"""End-to-end test for dbbackup.py. Run through tests/run_e2e.sh (it starts the containers)."""
import json
import os
import shutil
import subprocess
import sys
import tarfile
from datetime import datetime
from pathlib import Path

SCR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCR))
import dbbackup as d  # noqa: E402

ROOT = Path(os.environ.get("DBB_TEST_ROOT", "/tmp/dbbackup-e2e"))
shutil.rmtree(ROOT, ignore_errors=True)
d.BASE_DIR = ROOT
d.CONFIG_FILE = ROOT / "config.json"
d.BACKUP_ROOT = ROOT / "backups"
d.DAILY_DIR = d.BACKUP_ROOT / "daily"
d.WEEKLY_DIR = d.BACKUP_ROOT / "weekly"
d.MONTHLY_DIR = d.BACKUP_ROOT / "monthly"
d.YEARLY_DIR = d.BACKUP_ROOT / "yearly"
d.LOG_DIR = ROOT / "logs"
d.LOG_FILE = d.LOG_DIR / "dbbackup.log"
d.LOCK_FILE = ROOT / "dbbackup.lock"
d.DEPS_STAMP = ROOT / ".deps"

CH_PW = 'p@ss`w"rd<&>'
IMAGE = "clickhouse/clickhouse-server:25.8"
failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


ch_job = {
    "id": "11111111-aaaa-bbbb-cccc-000000000001", "name": "chtest",
    "database_type": "clickhouse", "host": "127.0.0.1", "port": 19900,
    "username": "tester", "password": CH_PW, "backup_all": True, "databases": [],
    "retention": {"daily_count": 1, "weekly_count": 2, "monthly_count": 3, "yearly_count": 3},
}
pg_job = {
    "id": "22222222-aaaa-bbbb-cccc-000000000002", "name": "pgtest",
    "database_type": "postgresql", "host": "127.0.0.1", "port": 15499,
    "username": "postgres", "password": "pgtest", "backup_all": False, "databases": ["shop"],
    "retention": {"daily_count": 1, "weekly_count": 2, "monthly_count": 3, "yearly_count": 3},
}
cfg = json.loads(json.dumps(d.DEFAULT_CONFIG))
cfg["jobs"] = [ch_job, pg_job]
d.save_config(cfg)

# 1. connection test + discovery (Docker client path: no clickhouse-client on host)
check(d.clickhouse_host_client() is None, "host has no clickhouse-client -> Docker client path")
ok, err = d.test_connection(d.job_connection(ch_job))
check(ok, f"clickhouse test_connection ({err[:120]})")
bad = dict(ch_job, password="wrong")
ok2, err2 = d.test_connection(d.job_connection(bad))
check(not ok2 and "Authentication" in err2 or "password" in err2.lower(), "wrong password is rejected")
ok, dbs, err = d.discover_databases(d.job_connection(ch_job))
check(ok and sorted(dbs) == ["analytics", "default", "sales db"], f"discovery {dbs} {err[:100]}")

# 2. run both jobs for daily+weekly in one run -> one dump, one hard link
moment = datetime(2026, 10, 11, 2, 0, 0)  # a Sunday
summaries = [d.run_job_backups(j, moment, ["daily", "weekly"]) for j in (ch_job, pg_job)]
for s in summaries:
    check(s["failed"] == 0, f"job {s['job']} failed=0 errors={s['errors'][:2]}")
    reused = [r for r in s["results"] if r.get("reused")]
    fresh = [r for r in s["results"] if not r.get("reused")]
    check(len(reused) == len(fresh), f"job {s['job']}: each weekly copy reused the daily dump")
    check(s["total_size"] == sum(r["size"] for r in fresh), f"job {s['job']}: total_size counts dumps once")

daily = sorted(p for p in d.DAILY_DIR.iterdir() if not p.name.endswith((".sha256", ".meta.json")))
weekly = sorted(p for p in d.WEEKLY_DIR.iterdir() if not p.name.endswith((".sha256", ".meta.json")))
check(len(daily) == len(weekly) == 5, f"5 artifacts per category (3 CH dbs + PG globals + shop): {len(daily)}/{len(weekly)}")
for a, b in zip(daily, weekly):
    check(a.stat().st_ino == b.stat().st_ino, f"hard link {a.name}")
    ma = json.loads(Path(f"{a}.meta.json").read_text())
    mb = json.loads(Path(f"{b}.meta.json").read_text())
    check(ma["sha256"] == mb["sha256"] == d.sha256_file(b), f"sha256 sidecar matches for {b.name}")
check(d.directory_size(d.BACKUP_ROOT) == sum(p.stat().st_size for p in daily)
      + sum(p.stat().st_size for p in d.DAILY_DIR.iterdir() if p.name.endswith((".sha256", ".meta.json")))
      + sum(p.stat().st_size for p in d.WEEKLY_DIR.iterdir() if p.name.endswith((".sha256", ".meta.json"))),
      "store total counts hard-linked dumps once")
log = d.LOG_FILE.read_text()
check(log.count("Starting backup | job=chtest") == 3, "clickhouse: 3 dumps, not 6")
check(log.count("Backup reused | job=chtest") == 3, "clickhouse: 3 reused")
check("p@ss" not in log, "password never written to the log")

# 3. archive layout + no leftover staging
sales = next(p for p in daily if "sales_db" in p.name)
check(sales.name.endswith(".ch.tar"), "extension .ch.tar")
names = tarfile.open(sales).getnames()
check({"manifest.json", "restore.py", "schema/000_database.sql"} <= set(names), "archive has manifest/restore/schema")
check(not any(p.name.startswith(".staging_") for p in d.DAILY_DIR.iterdir()), "staging dir removed")
check(d.verify_backup(sales, "clickhouse")[0], "verify_backup ok")

# 4. retention: second run on Monday (daily only) -> daily keeps 1, weekly keeps both links alive
d.run_job_backups(ch_job, datetime(2026, 10, 12, 2, 0, 0), ["daily"])
d.apply_retention(ch_job)
ch_daily = [p for p in d.DAILY_DIR.glob("chtest_*.ch.tar")]
ch_weekly = [p for p in d.WEEKLY_DIR.glob("chtest_*.ch.tar")]
check(len(ch_daily) == 3 and all("20261012" in p.name for p in ch_daily), "daily retention kept only newest")
check(len(ch_weekly) == 3 and all(p.stat().st_nlink == 1 for p in ch_weekly), "weekly copy survives deletion of its daily link")

# 5. restore into the empty second server and compare table checksums
restore_dir = ROOT / "restore"
restore_dir.mkdir()
sales = next(d.WEEKLY_DIR.glob("chtest_*sales_db_*.ch.tar"))
with tarfile.open(sales) as tf:
    tf.extractall(restore_dir)
shim = ROOT / "shim"
shim.mkdir()
(shim / "clickhouse-client").write_text(
    f'#!/bin/sh\nexec docker run --rm -i --network host {IMAGE} clickhouse-client "$@"\n')
(shim / "clickhouse-client").chmod(0o755)
env = dict(os.environ, PATH=f"{shim}:{os.environ['PATH']}")
r = subprocess.run([sys.executable, str(restore_dir / "restore.py"), "--host", "127.0.0.1",
                    "--port", "19901", "--user", "tester", "--password", CH_PW],
                   env=env, capture_output=True, text=True)
check(r.returncode == 0, f"restore.py rc=0 {r.stdout.strip()} {r.stderr.strip()[-300:]}")


def ch(container, q):
    return subprocess.run(["docker", "exec", container, "clickhouse-client", "--user", "tester",
                           "--password", CH_PW, "-q", q], capture_output=True, text=True).stdout.strip()


for table, cols in [("orders", "id, amount, d, note, amount_x2, label"), ("daily_to", "d, total"),
                    ("mv_daily", "d, total"), ("`weird ``name`", "k, v")]:
    q = f"SELECT count(), sum(cityHash64({cols})) FROM `sales db`.{table if table.startswith('`') else table}"
    if table in ("daily_to", "mv_daily"):
        q = f"SELECT count(), sum(cityHash64({cols})) FROM (SELECT d, sum(total) total FROM `sales db`.{table} GROUP BY d)"
    a, b = ch("dbb-ch-src", q), ch("dbb-ch-dst", q)
    check(a == b and a != "", f"restored {table}: src={a} dst={b}")
v = ch("dbb-ch-dst", "SELECT count() FROM `sales db`.v_big")
check(v == ch("dbb-ch-src", "SELECT count() FROM `sales db`.v_big"), f"view v_big works after restore ({v})")
# MV must still fire after restore, and must not have doubled data during restore
ch("dbb-ch-dst", "INSERT INTO `sales db`.orders (id, amount, d) VALUES (999999, 10, '2026-12-31')")
check(ch("dbb-ch-dst", "SELECT total = 10 FROM `sales db`.daily_to FINAL WHERE d = '2026-12-31'") == "1",
      "MV TO still fires after restore")

# 6. disk-space floor refuses to start a dump
cfg = d.load_config()
cfg["min_free_gb"] = 10 ** 6
d.save_config(cfg)
s = d.run_job_backups(pg_job, datetime(2026, 10, 13, 2, 0, 0), ["daily"])
check(s["failed"] == 2 and "Not enough free space" in s["errors"][0], f"disk floor blocks dumps: {s['errors'][:1]}")

print("\nRESULT:", "ALL PASS" if not failures else f"{len(failures)} FAILED")
sys.exit(1 if failures else 0)
