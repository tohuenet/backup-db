# DBBackup Usage Guide

Terminal-based database backup manager using whiptail menus.

## Starting the Application

```bash
sudo python3 /opt/dbbackup/dbbackup.py
```

## Main Menu

| Option | Description |
|--------|-------------|
| 1. Add Backup Job | Wizard to create a new backup job |
| 2. Edit Backup Job | Modify connection, databases, or retention |
| 3. Delete Backup Job | Remove a job (with confirmation) |
| 4. Run Backup Now | Execute backups immediately |
| 5. View Backup Usage | Show storage used by daily/weekly/monthly backups |
| 6. Telegram Settings | Configure bot token and chat ID |
| 7. Install Scheduler | Install/update systemd timer (02:00 daily) |
| 8. Show Logs | View recent log entries |
| 9. Exit | Close the application |

## Adding a Backup Job

The wizard guides you through:

1. **Database type** — PostgreSQL, MySQL/MariaDB, MongoDB, ArangoDB or ClickHouse
2. **Connection** — Host, port, username, password
3. **Connection test** — Must succeed before continuing
4. **Database discovery** — Lists databases on the remote server
5. **Selection mode** — Backup all databases or select manually
6. **Retention** — Daily (days), weekly (weeks), monthly (months)
7. **Save** — Job stored in `/opt/dbbackup/config.json`

Example retention values:

- Daily: `14` days
- Weekly: `8` weeks
- Monthly: `12` months

## Backup Output

Each database is backed up **separately** into its own file (one archive per database for MongoDB, ArangoDB and ClickHouse).

### File naming

```
<job>_<host>_<jobid8>_<database>_<YYYYmmdd_HHMMSS>.<ext>
pgprod_10.0.0.5_a1b2c3d4_customer_20260606_020000.sql.zst
globals_pgprod_10.0.0.5_a1b2c3d4_20260606_020000.sql.zst
chprod_10.0.0.6_d4e5f6a7_analytics_20260606_020000.ch.tar
```

### Sidecar files

Every backup produces three files:

```
pgprod_10.0.0.5_a1b2c3d4_customer_20260606_020000.sql.zst
pgprod_10.0.0.5_a1b2c3d4_customer_20260606_020000.sql.zst.sha256
pgprod_10.0.0.5_a1b2c3d4_customer_20260606_020000.sql.zst.meta.json
```

### Categories

| Category | Directory | When created |
|----------|-----------|--------------|
| Daily    | `/opt/dbbackup/backups/daily/`   | Every backup run |
| Weekly   | `/opt/dbbackup/backups/weekly/`  | Sundays |
| Monthly  | `/opt/dbbackup/backups/monthly/` | 1st of each month |
| Yearly   | `/opt/dbbackup/backups/yearly/`  | 1st of January |

Each database is dumped **once per run**. When a run covers several categories
(daily + weekly on Sunday, + monthly on the 1st), the other categories receive a
hard link to the same verified dump instead of a second dump against the
server. Retention counts each category independently; removing an old daily
copy leaves its weekly link intact. Storage totals count hard links once.

## Backup Tools Used

| Database   | Tool | Output |
|-----------|------|--------|
| PostgreSQL | `pg_dump` per database | `.sql.zst` |
| PostgreSQL globals | `pg_dumpall --globals-only` | `.sql.zst` |
| MySQL/MariaDB | `mysqldump --single-transaction` | `.sql.zst` |
| MongoDB | `mongodump` | `.tar.zst` |
| ArangoDB | `arangodump` | `.tar.zst` |
| ClickHouse | `clickhouse-client` (native protocol) | `.ch.tar` |

Clients run from the host when installed and version-compatible, otherwise
from a matching Docker image.

## ClickHouse Backups

One `.ch.tar` per database, containing:

```
manifest.json            tables, engines, stored columns, restore order
restore.py               restore script
schema/000_database.sql  CREATE DATABASE
schema/<tier>_<n>.sql    one CREATE per table / view / dictionary
data/<n>.native.zst      rows of every table that stores data (Native format, zstd)
```

- Data is exported for MergeTree-family tables and Log/Memory/Set/Join/
  EmbeddedRocksDB. Views, materialized views, dictionaries, Distributed and
  integration engines (Kafka, S3, MySQL, ...) are saved as schema only.
- Inner tables of materialized views are exported and reattached to the
  recreated view on restore.
- Tables are exported one at a time; each table is consistent, but tables are
  not frozen together (ClickHouse has no cross-table snapshot).
- Dictionary definitions whose source has a password are shown by ClickHouse
  with the password hidden; re-enter it after a restore.

### Restoring a ClickHouse backup

```bash
mkdir restore && tar -xf <backup>.ch.tar -C restore
python3 restore/restore.py --host 127.0.0.1 --port 9000 --user default --password '...'
```

Needs `clickhouse-client` and `zstd`. The database is recreated under its
original name. Materialized views are created after the data is loaded, so
reloading their source tables does not duplicate rows in their targets.

## Verification

After each backup:

- `.zst` files: `zstd -t`
- `.tar.zst` archives: `tar --zstd -tf`
- `.ch.tar` archives: every data member is checked with `zstd -t` while it is
  written, then the archive must list cleanly and contain `manifest.json` and
  `restore.py`

Failed verification deletes the backup, logs the failure, and sends a Telegram alert (if enabled).

A dump is not started when the backup filesystem has less than `min_free_gb`
GiB free (config key, default 20).

## Command-Line Usage

### Run all scheduled backups (used by systemd)

```bash
sudo python3 /opt/dbbackup/dbbackup.py --run-scheduled
```

### Run a specific job

```bash
sudo python3 /opt/dbbackup/dbbackup.py --run-job JOB_UUID
```

### Install dependencies only

```bash
sudo python3 /opt/dbbackup/dbbackup.py --install-deps
```

## Telegram Notifications

Enable from **Telegram Settings** in the main menu.

### Success message

- Server (host:port)
- Database name
- Duration
- Backup size

### Failure message

- Server (host:port)
- Database name
- Error details

### Daily summary (after scheduled run)

- Total backup size for the run
- Daily, weekly, monthly, and total storage usage

## Viewing Backup Usage

Menu option **5** shows filesystem usage:

- Daily size (bytes and human-readable)
- Weekly size
- Monthly size
- Total size

## Logs

All operations are logged to `/opt/dbbackup/logs/dbbackup.log`:

- Start and end times
- Job name and database
- Duration and file size
- Success or failure
- Errors and stack traces

View from the menu (**Show Logs**) or directly:

```bash
tail -100 /opt/dbbackup/logs/dbbackup.log
```

## Concurrent Execution

A lock file at `/opt/dbbackup/dbbackup.lock` prevents overlapping runs. If a backup is already in progress:

```
Backup already running.
```

## Retention

Old backups are deleted automatically after each job run. Retention is a
**copy count** per category and per database (`daily_count`, `weekly_count`,
`monthly_count`, `yearly_count`): the newest N are kept. Jobs created with the
older `daily_days`/`weekly_weeks`/`monthly_months` keys keep working; those
values are read as copy counts.

Retention is tracked via `.meta.json` sidecar files.

## Manual Restore

There is no restore menu. Restore SQL dumps manually as below; ClickHouse archives carry their own `restore.py` (see *Restoring a ClickHouse backup*).

### PostgreSQL

```bash
zstd -dc <...>_customer_<ts>.sql.zst | psql -h HOST -U USER -d customer
zstd -dc globals_<...>.sql.zst | psql -h HOST -U USER -d postgres
```

### MySQL

```bash
zstd -dc <...>_customer_<ts>.sql.zst | mysql -h HOST -u USER -p
```

### MongoDB

```bash
mkdir -p /tmp/restore && tar --zstd -xf <...>_customer_<ts>.tar.zst -C /tmp/restore
mongorestore --host HOST -u USER -p PASS --authenticationDatabase admin /tmp/restore
```

Always verify checksums before restore:

```bash
sha256sum -c <backup file>.sha256
```

## Systemd Management

```bash
# Check timer status
systemctl status dbbackup.timer

# View next scheduled run
systemctl list-timers dbbackup.timer

# Run backup manually via systemd
sudo systemctl start dbbackup.service

# View service output
journalctl -u dbbackup.service -f
```
