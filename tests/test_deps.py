"""Unit test: unattended runs only install clients the configured jobs need."""
import json, shutil, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import dbbackup as d
R = Path("/tmp/dbbackup-deps-test"); shutil.rmtree(R, ignore_errors=True)
d.BASE_DIR = R; d.CONFIG_FILE = R / "config.json"; d.LOG_DIR = R / "logs"; d.LOG_FILE = d.LOG_DIR / "x.log"
d.BACKUP_ROOT = R / "b"; d.DAILY_DIR = d.WEEKLY_DIR = d.MONTHLY_DIR = d.YEARLY_DIR = d.BACKUP_ROOT; d.DEPS_STAMP = R / ".s"
cfg = json.loads(json.dumps(d.DEFAULT_CONFIG))
cfg["jobs"] = [{"database_type": "postgresql"}, {"database_type": "clickhouse"}]
d.save_config(cfg)
d.command_exists = lambda c: False
d.package_installed = lambda p: False
d.mongodb_tools_available = lambda: False
fails = 0
for only, want_mongo, want in [(True, False, ["python3", "postgresql-client", "gzip", "zstd"]),
                               (False, True, ["python3", "whiptail", "postgresql-client", "mysql-client", "gzip", "zstd"])]:
    d.LOG_FILE.write_text("") if d.LOG_FILE.exists() else None
    d.install_dependencies(force_prompt=False, show_progress=False, only_for_jobs=only)
    line = [l for l in d.LOG_FILE.read_text().splitlines() if "Missing packages" in l][-1]
    got = line.split("Missing packages (no root): ")[1]
    ok = all(f"'{p}'" in got for p in want) and ("mongodb" in got) == want_mongo and ("mysql" in got) == (not only) and ("whiptail" in got) == (not only)
    print(("PASS" if ok else "FAIL"), f"only_for_jobs={only}: {got}")
    fails += not ok
sys.exit(fails)
