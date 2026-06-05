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

1. **Database type** — PostgreSQL, MySQL/MariaDB, or MongoDB
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

Each database is backed up **separately**. Files are never combined into a single archive (except MongoDB per-database tar.gz archives).

### File naming

```
customer_20260606_020000.sql.gz
billing_20260606_020000.sql.gz
globals_ProductionPostgreSQL_db.example.com_a1b2c3d4_20260606_020000.sql.gz
```

### Sidecar files

Every backup produces three files:

```
customer_20260606_020000.sql.gz
customer_20260606_020000.sql.gz.sha256
customer_20260606_020000.sql.gz.meta.json
```

### Categories

| Category | Directory | When created |
|----------|-----------|--------------|
| Daily    | `/opt/dbbackup/backups/daily/`   | Every backup run |
| Weekly   | `/opt/dbbackup/backups/weekly/`  | Sundays |
| Monthly  | `/opt/dbbackup/backups/monthly/` | 1st of each month |

## Backup Tools Used

| Database   | Tool | Compression |
|-----------|------|-------------|
| PostgreSQL | `pg_dump` per database | gzip |
| PostgreSQL globals | `pg_dumpall --globals-only` | gzip |
| MySQL/MariaDB | `mysqldump --single-transaction` | gzip |
| MongoDB | `mongodump` | tar.gz |

## Verification

After each backup:

- **gzip** files: verified with `gzip -t`
- **tar.gz** files: verified with `tar -tzf`

Failed verification deletes the backup, logs the failure, and sends a Telegram alert (if enabled).

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

Old backups are deleted automatically after each job run, per job settings:

- **Daily** backups older than N days
- **Weekly** backups older than N weeks
- **Monthly** backups older than N months

Retention is tracked via `.meta.json` sidecar files.

## Manual Restore

Restore is **not** implemented in this application. Restore backups manually:

### PostgreSQL

```bash
gunzip -c customer_20260606_020000.sql.gz | psql -h HOST -U USER -d customer
gunzip -c globals_*.sql.gz | psql -h HOST -U USER -d postgres
```

### MySQL

```bash
gunzip -c customer_20260606_020000.sql.gz | mysql -h HOST -u USER -p
```

### MongoDB

```bash
tar -xzf customer_20260606_020000.tar.gz -C /tmp/restore
mongorestore --host HOST -u USER -p PASS --authenticationDatabase admin /tmp/restore
```

Always verify checksums before restore:

```bash
sha256sum -c customer_20260606_020000.sql.gz.sha256
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
