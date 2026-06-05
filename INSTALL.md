# DBBackup Installation Guide

Enterprise Database Backup Manager for **Ubuntu 22.04 LTS**.

## Requirements

- Ubuntu 22.04 LTS
- Root or sudo access
- Network access to target database servers
- Terminal with whiptail support

## Quick Install

### 1. Copy the application

```bash
sudo mkdir -p /opt/dbbackup
sudo cp dbbackup.py /opt/dbbackup/dbbackup.py
sudo chmod 755 /opt/dbbackup/dbbackup.py
```

### 2. Install dependencies

The application installs missing packages automatically when run as root. To install dependencies only:

```bash
sudo python3 /opt/dbbackup/dbbackup.py --install-deps
```

Packages from Ubuntu default repositories are installed directly. **MongoDB tools** (`mongodb-database-tools`, `mongodb-mongosh`) are not in Ubuntu default repos; the installer automatically adds the [MongoDB 7.0 official apt repository](https://www.mongodb.com/docs/manual/tutorial/install-mongodb-on-ubuntu/) and falls back to a direct `.deb` download if needed.

| Command    | Package                  |
|-----------|--------------------------|
| python3   | python3                  |
| whiptail  | whiptail                 |
| pg_dump   | postgresql-client        |
| mysqldump | mysql-client             |
| mongodump   | mongodb-database-tools (MongoDB official repo) |
| mongosh     | mongodb-mongosh (MongoDB official repo)       |
| gzip      | gzip                     |

### 3. Launch the application

```bash
sudo python3 /opt/dbbackup/dbbackup.py
```

Running as root is recommended for:

- Installing packages
- Writing to `/opt/dbbackup/`
- Installing the systemd timer
- Reading database credentials from remote servers

### 4. Install the scheduler

From the main menu, choose **Install Scheduler**, or install manually:

```bash
sudo cp examples/dbbackup.service /etc/systemd/system/dbbackup.service
sudo cp examples/dbbackup.timer /etc/systemd/system/dbbackup.timer
sudo systemctl daemon-reload
sudo systemctl enable dbbackup.timer
sudo systemctl start dbbackup.timer
```

Verify the timer:

```bash
systemctl status dbbackup.timer
systemctl list-timers dbbackup.timer
```

Default schedule: **02:00 every day**.

## Directory Layout

Created automatically on first run:

```
/opt/dbbackup/
├── dbbackup.py          # Application (copied during scheduler install)
├── config.json          # Job and Telegram configuration
├── dbbackup.lock        # Lock file (runtime)
├── backups/
│   ├── daily/
│   ├── weekly/
│   └── monthly/
└── logs/
    └── dbbackup.log
```

## Configuration File

Configuration is stored in `/opt/dbbackup/config.json` as plaintext JSON. Passwords are stored in plaintext by design.

See `examples/config.json` for a full example.

## Database Client Notes

### PostgreSQL

Ensure the backup user can connect remotely and has sufficient privileges for `pg_dump` and `pg_dumpall --globals-only`.

### MySQL / MariaDB

Ensure the backup user has `SELECT`, `SHOW VIEW`, `TRIGGER`, and `EVENT` privileges on target databases.

### MongoDB

Ensure the backup user can authenticate against the `admin` database and has read access to target databases. The application uses `mongosh` with fallback to legacy `mongo`.

## Firewall

Allow outbound connections from the backup server to remote database hosts on the configured ports (5432, 3306, 27017 by default).

## Telegram (Optional)

Configure from the main menu under **Telegram Settings**. A successful test message is required before settings are saved.

## Troubleshooting

| Issue | Action |
|-------|--------|
| `whiptail is not installed` | Run with `sudo` to auto-install dependencies |
| `Backup already running` | Another instance is active; check `/opt/dbbackup/dbbackup.lock` |
| Connection test fails | Verify host, port, credentials, and firewall rules |
| Timer not firing | Run `systemctl status dbbackup.timer` and check journal logs |

View application logs:

```bash
tail -f /opt/dbbackup/logs/dbbackup.log
journalctl -u dbbackup.service
```

## Security Notes

- Passwords are stored in plaintext in `config.json`
- No restore functionality is provided (intentional)
- Restrict file permissions on `/opt/dbbackup/config.json`:

```bash
sudo chmod 600 /opt/dbbackup/config.json
```
