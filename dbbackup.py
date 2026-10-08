#!/usr/bin/env python3
"""
DBBackup - Enterprise Database Backup Manager

Single-file terminal application for managing PostgreSQL, MySQL/MariaDB,
MongoDB, ArangoDB and ClickHouse backups on Ubuntu 22.04+ LTS. Uses whiptail
for UI and systemd timers for scheduling.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import html
import http.server
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------

BASE_DIR = Path("/opt/dbbackup")
CONFIG_FILE = BASE_DIR / "config.json"
BACKUP_ROOT = BASE_DIR / "backups"
DAILY_DIR = BACKUP_ROOT / "daily"
WEEKLY_DIR = BACKUP_ROOT / "weekly"
MONTHLY_DIR = BACKUP_ROOT / "monthly"
YEARLY_DIR = BACKUP_ROOT / "yearly"
LOG_DIR = BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "dbbackup.log"
LOCK_FILE = BASE_DIR / "dbbackup.lock"
DEPS_STAMP = BASE_DIR / ".deps_verified"

SYSTEMD_SERVICE = Path("/etc/systemd/system/dbbackup.service")
SYSTEMD_TIMER = Path("/etc/systemd/system/dbbackup.timer")

APP_TITLE = "DBBackup - Enterprise Database Backup Manager"
SCRIPT_PATH = Path(__file__).resolve()

DB_TYPES = {
    "postgresql": "PostgreSQL",
    "mysql": "MySQL/MariaDB",
    "mongodb": "MongoDB",
    "arangodb": "ArangoDB",
    "clickhouse": "ClickHouse",
}

# Built-in system databases excluded from automatic "backup all" discovery and
# from the manual selection list. Names are lowercase; matching is
# case-insensitive.
#   - MySQL: information_schema/performance_schema are virtual (mysqldump cannot
#     dump them and errors out); sys is just auto-generated views. 'mysql' (users
#     and grants) is intentionally kept as it holds real data.
#   - MongoDB: admin/config are cluster metadata; local holds the oplog and must
#     not be dumped.
#   - PostgreSQL: templates are already excluded by the discovery query, and
#     'postgres' may legitimately hold data, so nothing extra is filtered.
#   - ArangoDB: _system holds users, graphs and Foxx services (server metadata);
#     excluded from auto-discovery, but can still be backed up if selected
#     manually.
#   - ClickHouse: system and information_schema (both spellings) are virtual
#     server metadata. 'default' is kept because it often holds real tables.
SYSTEM_DATABASES: Dict[str, set] = {
    "postgresql": set(),
    "mysql": {"information_schema", "performance_schema", "sys"},
    "mongodb": {"admin", "local", "config"},
    "arangodb": {"_system"},
    "clickhouse": {"system", "information_schema"},
}

APT_PACKAGES = {
    "python3": "python3",
    "whiptail": "whiptail",
    "pg_dump": "postgresql-client",
    "mysqldump": "mysql-client",
    "gzip": "gzip",
    "zstd": "zstd",
}

# MongoDB tools are not in Ubuntu default repos; installed via MongoDB apt repo.
MONGODB_APT_PACKAGES = ["mongodb-database-tools", "mongodb-mongosh"]

MONGODB_REPO_KEYRING = Path("/usr/share/keyrings/mongodb-server-7.0.gpg")
MONGODB_REPO_LIST = Path("/etc/apt/sources.list.d/mongodb-org-7.0.list")

# Pinned fallback .deb when apt repository setup is unavailable.
MONGODB_DEB_VERSION = "100.10.0"
MONGODB_DEB_ARCH = {
    "x86_64": "x86_64",
    "amd64": "x86_64",
    "aarch64": "arm64",
    "arm64": "arm64",
}
MONGODB_UBUNTU_RELEASE = {
    "jammy": "2204",
    "focal": "2004",
    "noble": "2404",
}

# ArangoDB ships no client package in Ubuntu repos. We back it up with
# `arangodump`, preferring a native binary if present on PATH and otherwise
# running it from the official ArangoDB Docker image over the network (the
# server's HTTP endpoint, e.g. tcp://host:8529). The image tag should match the
# target server's major.minor version; a per-job "arango_image" overrides this.
ARANGO_DOCKER_IMAGE = "arangodb:3.8.4"

# Compression: zstd replaces gzip — better ratio, much faster, multi-threaded.
# Level is tunable; -12 is a good size/speed balance for large dumps.
ZSTD_LEVEL = "-12"
ZSTD_THREADS = "-T0"

# Docker images used when no compatible host client is available. The backup
# auto-detects per job: it uses the host binary when present and version-
# compatible, otherwise it runs the client from a version-matched container so
# the host needs no database client packages.
MYSQL_DOCKER_IMAGE = "mysql:8"
# The official `mongo` image bundles mongodump/mongorestore (the standalone
# tools image is not published on Docker Hub).
MONGO_TOOLS_DOCKER_IMAGE = "mongo:7"
DEFAULT_PG_IMAGE_MAJOR = 16
# The server image bundles clickhouse-client. The native protocol is backward
# compatible, so one recent LTS client talks to older servers; a per-job
# "clickhouse_image" overrides it.
CLICKHOUSE_DOCKER_IMAGE = "clickhouse/clickhouse-server:25.8"

# ClickHouse engines whose rows are stored by ClickHouse itself and therefore
# exported. Every other engine (View, MaterializedView, Dictionary,
# Distributed, Kafka, S3, URL, MySQL, PostgreSQL, ...) is backed up as schema
# only: its data is either derived or lives in another system.
CLICKHOUSE_DATA_ENGINES = {
    "Log", "TinyLog", "StripeLog", "Memory", "Set", "Join", "EmbeddedRocksDB",
}

# Refuse to start a dump when the backup filesystem has less free space than
# this (GiB). Protects hosts that share the disk with other services (a full
# root filesystem stops mail, databases, logging...). Override per install
# with the top-level config key "min_free_gb".
DEFAULT_MIN_FREE_GB = 20

# Default retention (number of copies kept per category) for new jobs.
DEFAULT_RETENTION: Dict[str, int] = {
    "daily_count": 3,
    "weekly_count": 3,
    "monthly_count": 3,
    "yearly_count": 3,
}

DEFAULT_CONFIG: Dict[str, Any] = {
    "telegram": {
        "enabled": False,
        "bot_token": "",
        "chat_id": "",
        # Notification sound: when True the message is delivered silently
        # (no sound/vibration). Successes are commonly muted (one ping per
        # database is noisy) while failures ring.
        "silent_success": False,
        "silent_failure": False,
        # When False (default), backups send ONE consolidated run report instead
        # of a separate message per database. Set True for the old per-database
        # notifications.
        "per_database_notifications": False,
    },
    # Web dashboard auth. The password is stored as a salted PBKDF2 hash, never
    # in plaintext. Set it with `--set-web-password` (or the menu).
    "web": {
        "username": "admin",
        "salt": "",
        "password_hash": "",
        "iterations": 200000,
    },
    "jobs": [],
}

# PBKDF2 work factor for the web dashboard password.
WEB_PBKDF2_ITERATIONS = 200000

# Fallback when whiptail cannot access the TTY (common under sudo/SSH).
TEXT_UI_MODE = False

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def ensure_directories() -> None:
    """Create required directory structure under /opt/dbbackup."""
    for directory in (BASE_DIR, DAILY_DIR, WEEKLY_DIR, MONTHLY_DIR, YEARLY_DIR, LOG_DIR):
        directory.mkdir(parents=True, exist_ok=True)
    if not LOG_FILE.exists():
        LOG_FILE.touch(mode=0o644)


def log_message(message: str, level: str = "INFO") -> None:
    """Append a timestamped line to the application log file."""
    ensure_directories()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] [{level}] {message}\n"
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError as exc:
        print(f"Failed to write log: {exc}", file=sys.stderr)


def log_exception(context: str, exc: BaseException) -> None:
    """Log an exception with full traceback."""
    log_message(f"{context}: {exc}", "ERROR")
    log_message(traceback.format_exc(), "ERROR")


def console_msg(message: str) -> None:
    """Print a progress line to the terminal (immediate feedback for the user)."""
    print(f"[dbbackup] {message}", file=sys.stderr, flush=True)


def run_subprocess(
    command: List[str],
    show_progress: bool = True,
    step: str = "",
) -> subprocess.CompletedProcess:
    """
    Run a subprocess. When show_progress is True, inherit the terminal so the
    user can see apt/curl output instead of a silent hang.
    """
    if step and show_progress:
        console_msg(step)
    if show_progress:
        return subprocess.run(command, check=False)
    return subprocess.run(command, check=False, capture_output=True, text=True)


def ensure_interactive_terminal() -> bool:
    """Verify stdin/stdout are TTYs (required for whiptail menus)."""
    if sys.stdin.isatty() and sys.stdout.isatty():
        if not os.environ.get("TERM"):
            os.environ["TERM"] = "linux"
        return True
    console_msg("ERROR: Interactive mode requires a real terminal (TTY).")
    console_msg("If using SSH, connect with: ssh -t user@host")
    console_msg("Then run: sudo python3 /opt/dbbackup/dbbackup.py")
    return False


# ---------------------------------------------------------------------------
# Whiptail UI helpers
# ---------------------------------------------------------------------------


def whiptail_available() -> bool:
    """Return True if whiptail is installed and reachable."""
    return shutil.which("whiptail") is not None


def open_controlling_tty():
    """
    Open the controlling terminal (/dev/tty).

    Required when running under sudo — sys.stdin/stdout may not be the TTY
    that whiptail must draw on, which causes invisible dialogs and apparent hangs.
    """
    try:
        return open("/dev/tty", "r+b", buffering=0)
    except OSError:
        return None


def whiptail_supports_output_fd() -> bool:
    """Detect whether this whiptail build supports --output-fd."""
    try:
        result = subprocess.run(
            ["whiptail", "--help"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return "--output-fd" in f"{result.stdout}\n{result.stderr}"
    except OSError:
        return False


_WHIPTAIL_OUTPUT_FD: Optional[bool] = None


def run_whiptail(args: List[str], timeout: int = 0) -> Tuple[int, str]:
    """
    Execute whiptail and return (exit_code, selection).

    Uses --output-fd with /dev/tty so the dialog is visible even under sudo.
    Falls back to the bash fd-swap method without capturing stderr.
    """
    global _WHIPTAIL_OUTPUT_FD
    if _WHIPTAIL_OUTPUT_FD is None:
        _WHIPTAIL_OUTPUT_FD = whiptail_supports_output_fd()

    tty = open_controlling_tty()
    stdin = tty if tty is not None else sys.stdin
    stdout = tty if tty is not None else sys.stdout
    stderr = tty if tty is not None else sys.stderr

    env = os.environ.copy()
    env.setdefault("TERM", "xterm-256color")
    env.setdefault("LANG", "C.UTF-8")

    base_args = ["--backtitle", APP_TITLE] + args

    try:
        if _WHIPTAIL_OUTPUT_FD:
            return _run_whiptail_output_fd(
                base_args, stdin, stdout, stderr, env, timeout, tty
            )
        return _run_whiptail_fd_swap(
            base_args, stdin, stdout, stderr, env, timeout, tty
        )
    finally:
        if tty is not None:
            tty.close()


def _run_whiptail_output_fd(
    args: List[str],
    stdin,
    stdout,
    stderr,
    env: Dict[str, str],
    timeout: int,
    tty,
) -> Tuple[int, str]:
    """Run whiptail with --output-fd (recommended; works with sudo)."""
    read_fd, write_fd = os.pipe()
    command = ["whiptail", "--output-fd", str(write_fd)] + args
    try:
        proc = subprocess.Popen(
            command,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            env=env,
            pass_fds=(write_fd,),
            close_fds=True,
        )
    except OSError as exc:
        os.close(read_fd)
        os.close(write_fd)
        log_exception("whiptail Popen failed", exc)
        return 1, ""
    os.close(write_fd)
    output_chunks: List[bytes] = []
    try:
        while True:
            try:
                chunk = os.read(read_fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            output_chunks.append(chunk)
    finally:
        os.close(read_fd)

    try:
        returncode = proc.wait(timeout=timeout if timeout > 0 else None)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        log_message("whiptail timed out (UI may be invisible — check TTY/sudo)", "ERROR")
        return 1, ""

    selection = b"".join(output_chunks).decode("utf-8", errors="replace").strip()
    return returncode, selection


def _run_whiptail_fd_swap(
    args: List[str],
    stdin,
    stdout,
    stderr,
    env: Dict[str, str],
    timeout: int,
    tty,
) -> Tuple[int, str]:
    """Fallback for older whiptail without --output-fd."""
    command = ["whiptail"] + args
    bash_cmd = " ".join(shlex.quote(part) for part in command) + " 3>&1 1>&2 2>&3"
    try:
        result = subprocess.run(
            ["bash", "-c", bash_cmd],
            stdout=subprocess.PIPE,
            stderr=stderr,
            stdin=stdin,
            text=True,
            env=env,
            timeout=timeout if timeout > 0 else None,
            check=False,
        )
    except subprocess.TimeoutExpired:
        log_message("whiptail timed out (UI may be invisible — check TTY/sudo)", "ERROR")
        return 1, ""
    return result.returncode, (result.stdout or "").strip()


def test_whiptail_ui() -> bool:
    """Return True if a whiptail dialog can be shown and dismissed."""
    if not whiptail_available():
        console_msg("whiptail binary not found.")
        return False
    code, _ = run_whiptail(
        [
            "--title",
            "DBBackup",
            "--msgbox",
            "Whiptail UI test OK.\n\nPress OK to continue.",
            "10",
            "50",
        ],
        timeout=120,
    )
    return code == 0


def enable_text_ui(reason: str) -> None:
    """Switch all UI helpers to plain terminal prompts."""
    global TEXT_UI_MODE
    TEXT_UI_MODE = True
    console_msg(f"Using text menu mode ({reason}).")


def msg_box(title: str, message: str, height: int = 10, width: int = 70) -> None:
    """Display an informational message box."""
    if TEXT_UI_MODE:
        print(f"\n=== {title} ===\n{message}\n", flush=True)
        try:
            input("Press Enter to continue...")
        except EOFError:
            pass
        return
    run_whiptail(
        [
            "--title",
            title,
            "--msgbox",
            message,
            str(height),
            str(width),
        ]
    )


def yes_no(title: str, message: str, default: str = "yes") -> bool:
    """Display a yes/no dialog. Returns True for Yes."""
    if TEXT_UI_MODE:
        default_hint = "Y/n" if default == "yes" else "y/N"
        print(f"\n=== {title} ===\n{message}\n", flush=True)
        try:
            answer = input(f"Yes or No? [{default_hint}]: ").strip().lower()
        except EOFError:
            return default == "yes"
        if not answer:
            return default == "yes"
        return answer in ("y", "yes")
    code, _ = run_whiptail(
        [
            "--title",
            title,
            "--yesno",
            message,
            "10",
            "70",
            "--default-no" if default == "no" else "--default-yes",
        ]
    )
    return code == 0


def input_box(
    title: str,
    prompt: str,
    default: str = "",
    password: bool = False,
) -> Optional[str]:
    """Display a text input box. Returns None if cancelled."""
    if TEXT_UI_MODE:
        print(f"\n=== {title} ===", flush=True)
        suffix = f" [{default}]" if default and not password else ""
        try:
            if password:
                value = getpass.getpass(f"{prompt}{suffix}: ")
            else:
                value = input(f"{prompt}{suffix}: ").strip()
        except EOFError:
            return None
        if not value and default and not password:
            return default
        if value.lower() in ("q", "quit", "cancel") and not password:
            return None
        return value
    args = [
        "--title",
        title,
        "--inputbox",
        prompt,
        "10",
        "70",
    ]
    if default:
        args.append(default)
    if password:
        args = [
            "--title",
            title,
            "--passwordbox",
            prompt,
            "10",
            "70",
        ]
        if default:
            args.append(default)
    code, value = run_whiptail(args)
    if code != 0:
        return None
    return value


def menu(title: str, items: List[Tuple[str, str]], height: int = 20) -> Optional[str]:
    """
    Display a menu. items is list of (tag, description).
    Returns selected tag or None if cancelled.
    """
    if not items:
        msg_box(title, "No items available.")
        return None
    if TEXT_UI_MODE:
        print(f"\n=== {title} ===", flush=True)
        tags = []
        for index, (tag, description) in enumerate(items, start=1):
            tags.append(tag)
            print(f"  {index}. {description}  [{tag}]", flush=True)
        try:
            raw = input("Enter number (or 'q' to cancel): ").strip().lower()
        except EOFError:
            return None
        if raw in ("q", "quit", ""):
            return None
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(tags):
                return tags[idx]
        for tag, _desc in items:
            if raw == tag.lower() or raw == tag:
                return tag
        msg_box("Invalid", "Invalid selection.")
        return None
    menu_args = ["--title", title, "--menu", "Select an option:", str(height), "70", "10"]
    for tag, description in items:
        menu_args.extend([tag, description])
    code, selection = run_whiptail(menu_args)
    if code != 0:
        return None
    return selection


def checklist(
    title: str,
    prompt: str,
    items: List[Tuple[str, str, bool]],
) -> Optional[List[str]]:
    """
    Display a checkbox list. items: (tag, description, selected).
    Returns list of selected tags or None if cancelled.
    """
    if not items:
        msg_box(title, "No databases available to select.")
        return None
    if TEXT_UI_MODE:
        print(f"\n=== {title} ===\n{prompt}\n", flush=True)
        tags = []
        for index, (tag, description, selected) in enumerate(items, start=1):
            tags.append(tag)
            mark = "x" if selected else " "
            print(f"  {index}. [{mark}] {description}  ({tag})", flush=True)
        try:
            raw = input("Enter numbers separated by comma (or 'q' to cancel): ").strip()
        except EOFError:
            return None
        if raw.lower() in ("q", "quit", ""):
            return None
        chosen: List[str] = []
        for part in raw.split(","):
            part = part.strip()
            if part.isdigit():
                idx = int(part) - 1
                if 0 <= idx < len(tags):
                    chosen.append(tags[idx])
            elif part in tags:
                chosen.append(part)
        return chosen
    args = ["--title", title, "--checklist", prompt, "20", "70", "10"]
    for tag, description, selected in items:
        state = "ON" if selected else "OFF"
        args.extend([tag, description, state])
    code, selection = run_whiptail(args)
    if code != 0:
        return None
    if not selection:
        return []
    selected: List[str] = []
    for token in selection.split('" "'):
        token = token.strip('"').strip()
        if token:
            selected.append(token)
    # whiptail returns quoted tokens like "db1" "db2"
    parsed = re.findall(r'"([^"]+)"', selection)
    return parsed if parsed else selected


def radiolist(
    title: str,
    prompt: str,
    items: List[Tuple[str, str, bool]],
) -> Optional[str]:
    """Display a radiolist. Returns selected tag or None."""
    if TEXT_UI_MODE:
        print(f"\n=== {title} ===\n{prompt}\n", flush=True)
        tags = []
        for index, (tag, description, selected) in enumerate(items, start=1):
            tags.append(tag)
            mark = "*" if selected else " "
            print(f"  {index}. ({mark}) {description}  [{tag}]", flush=True)
        try:
            raw = input("Enter number (or 'q' to cancel): ").strip().lower()
        except EOFError:
            return None
        if raw in ("q", "quit", ""):
            return None
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(tags):
                return tags[idx]
        for tag in tags:
            if raw == tag.lower() or raw == tag:
                return tag
        return None
    args = ["--title", title, "--radiolist", prompt, "12", "70", "6"]
    for tag, description, selected in items:
        state = "ON" if selected else "OFF"
        args.extend([tag, description, state])
    code, selection = run_whiptail(args)
    if code != 0:
        return None
    parsed = re.findall(r'"([^"]+)"', selection)
    if parsed:
        return parsed[0]
    return selection.strip('"').strip() or None


def scroll_box(title: str, content: str) -> None:
    """Display scrollable text."""
    if not content.strip():
        content = "(empty)"
    if TEXT_UI_MODE:
        print(f"\n=== {title} ===\n{content}\n", flush=True)
        try:
            input("Press Enter to continue...")
        except EOFError:
            pass
        return
    lines = content.count("\n") + 1
    height = min(max(lines + 2, 10), 30)
    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(content)
        tmp_path = tmp.name
    try:
        run_whiptail(
            [
                "--title",
                title,
                "--scrolltext",
                "--textbox",
                tmp_path,
                str(height),
                "78",
            ]
        )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def load_config() -> Dict[str, Any]:
    """Load configuration from JSON file, creating default if missing."""
    ensure_directories()
    if not CONFIG_FILE.exists():
        save_config(DEFAULT_CONFIG.copy())
        return json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as handle:
            config = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        log_exception("Failed to load config", exc)
        config = json.loads(json.dumps(DEFAULT_CONFIG))
    if "telegram" not in config:
        config["telegram"] = DEFAULT_CONFIG["telegram"].copy()
    if "web" not in config:
        config["web"] = DEFAULT_CONFIG["web"].copy()
    if "jobs" not in config:
        config["jobs"] = []
    return config


def save_config(config: Dict[str, Any]) -> bool:
    """Persist configuration to JSON file."""
    ensure_directories()
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as handle:
            json.dump(config, handle, indent=2)
            handle.write("\n")
        # Config stores plaintext DB passwords — restrict to owner only.
        try:
            os.chmod(CONFIG_FILE, 0o600)
        except OSError:
            pass
        return True
    except OSError as exc:
        log_exception("Failed to save config", exc)
        msg_box("Error", f"Could not save configuration:\n{exc}")
        return False


def find_job(config: Dict[str, Any], job_id: str) -> Optional[Dict[str, Any]]:
    """Find a job dict by its id."""
    for job in config.get("jobs", []):
        if job.get("id") == job_id:
            return job
    return None


# ---------------------------------------------------------------------------
# Dependency management
# ---------------------------------------------------------------------------


def command_exists(command: str) -> bool:
    """Check whether an executable is available on PATH."""
    return shutil.which(command) is not None


def package_installed(package: str) -> bool:
    """Check whether an apt package is installed."""
    try:
        result = subprocess.run(
            ["dpkg-query", "-W", "-f=${Status}", package],
            capture_output=True,
            text=True,
            check=False,
        )
        return "install ok installed" in result.stdout
    except OSError:
        return False


def detect_ubuntu_codename() -> Optional[str]:
    """Return Ubuntu release codename (e.g. jammy) or None if not Ubuntu."""
    try:
        with open("/etc/os-release", "r", encoding="utf-8") as handle:
            data: Dict[str, str] = {}
            for line in handle:
                if "=" in line:
                    key, value = line.strip().split("=", 1)
                    data[key] = value.strip('"')
        if data.get("ID") != "ubuntu":
            return None
        return data.get("VERSION_CODENAME") or None
    except OSError:
        return None


def mongodb_tools_available() -> bool:
    """Return True if mongodump is installed and usable."""
    return command_exists("mongodump")


def mongodb_repo_configured() -> bool:
    """Return True if a MongoDB apt source list is present."""
    sources_dir = Path("/etc/apt/sources.list.d")
    if not sources_dir.is_dir():
        return False
    return any(sources_dir.glob("mongodb-org-*.list"))


def ensure_mongodb_apt_repo(show_progress: bool = True) -> bool:
    """
    Add MongoDB 7.0 official apt repository for Ubuntu.

    mongodb-database-tools is not shipped in Ubuntu default repositories.
    """
    if MONGODB_REPO_LIST.exists():
        return True

    codename = detect_ubuntu_codename() or "jammy"
    repo_line = (
        f"deb [ arch=amd64,arm64 signed-by={MONGODB_REPO_KEYRING} ] "
        f"https://repo.mongodb.org/apt/ubuntu {codename}/mongodb-org/7.0 multiverse\n"
    )

    try:
        subprocess.run(
            ["install", "-d", "-m", "0755", "/usr/share/keyrings"],
            check=True,
        )
        for command, package in (
            ("curl", "curl"),
            ("gpg", "gnupg"),
            ("ca-certificates", "ca-certificates"),
        ):
            if not command_exists(command) and not package_installed(package):
                result = run_subprocess(
                    ["apt-get", "install", "-y", package],
                    show_progress=show_progress,
                    step=f"Installing helper package: {package}...",
                )
                if result.returncode != 0:
                    raise subprocess.CalledProcessError(result.returncode, "apt-get")
        if show_progress:
            console_msg("Adding MongoDB GPG key...")
        subprocess.run(
            [
                "bash",
                "-c",
                "curl -fsSL https://www.mongodb.org/static/pgp/server-7.0.asc | "
                f"gpg --dearmor -o {MONGODB_REPO_KEYRING}",
            ],
            check=True,
        )
        MONGODB_REPO_LIST.write_text(repo_line, encoding="utf-8")
        log_message(f"Added MongoDB apt repository ({codename})")
        if show_progress:
            console_msg(f"MongoDB apt repository added ({codename}/mongodb-org/7.0)")
        return True
    except (OSError, subprocess.CalledProcessError) as exc:
        log_exception("Failed to configure MongoDB apt repository", exc)
        return False


def mongodb_deb_download_url() -> Optional[str]:
    """Build MongoDB database tools .deb download URL for this system."""
    import platform

    codename = detect_ubuntu_codename() or "jammy"
    ubuntu_release = MONGODB_UBUNTU_RELEASE.get(codename, "2204")
    arch = MONGODB_DEB_ARCH.get(platform.machine().lower())
    if not arch:
        return None
    return (
        "https://fastdl.mongodb.org/tools/db/"
        f"mongodb-database-tools-ubuntu{ubuntu_release}-{arch}-"
        f"{MONGODB_DEB_VERSION}.deb"
    )


def install_mongodb_tools_from_deb(show_progress: bool = True) -> bool:
    """Fallback installer using official MongoDB .deb package."""
    url = mongodb_deb_download_url()
    if not url:
        log_message("Unsupported architecture for MongoDB tools .deb fallback", "ERROR")
        return False

    log_message(f"Installing MongoDB tools from .deb: {url}")
    if show_progress:
        console_msg("Downloading MongoDB database tools (.deb)...")
    with tempfile.TemporaryDirectory(prefix="dbbackup_mongo_deb_") as temp_dir:
        deb_path = Path(temp_dir) / "mongodb-database-tools.deb"
        try:
            result = run_subprocess(
                ["curl", "-fSL", "-o", str(deb_path), url],
                show_progress=show_progress,
            )
            if result.returncode != 0:
                raise subprocess.CalledProcessError(result.returncode, "curl")
            result = run_subprocess(
                ["apt-get", "install", "-y", str(deb_path)],
                show_progress=show_progress,
                step="Installing MongoDB database tools from .deb...",
            )
            if result.returncode != 0:
                raise subprocess.CalledProcessError(result.returncode, "apt-get")
        except subprocess.CalledProcessError as exc:
            log_exception("MongoDB .deb installation failed", exc)
            return False

    if mongodb_tools_available():
        log_message("MongoDB database tools installed from .deb")
        return True
    log_message("mongodump still unavailable after .deb install", "ERROR")
    return False


def install_mongodb_tools(show_progress: bool = True) -> bool:
    """Install mongodump/mongosh via MongoDB apt repo or .deb fallback."""
    if mongodb_tools_available():
        return True

    if os.geteuid() != 0:
        log_message("MongoDB tools install requires root", "WARNING")
        return False

    if not mongodb_repo_configured():
        if show_progress:
            console_msg("Configuring MongoDB official apt repository...")
        if not ensure_mongodb_apt_repo(show_progress=show_progress):
            return install_mongodb_tools_from_deb(show_progress=show_progress)

    try:
        result = run_subprocess(
            ["apt-get", "update"],
            show_progress=show_progress,
            step="Updating apt cache (MongoDB repository)...",
        )
        if result.returncode != 0:
            raise subprocess.CalledProcessError(result.returncode, "apt-get update")
        result = run_subprocess(
            ["apt-get", "install", "-y"] + MONGODB_APT_PACKAGES,
            show_progress=show_progress,
            step="Installing mongodb-database-tools and mongodb-mongosh...",
        )
        if result.returncode != 0:
            raise subprocess.CalledProcessError(result.returncode, "apt-get install")
    except subprocess.CalledProcessError as exc:
        log_message(
            f"MongoDB apt install failed, trying .deb fallback: {exc}",
            "WARNING",
        )
        if show_progress:
            console_msg("MongoDB apt install failed, trying .deb fallback...")
        return install_mongodb_tools_from_deb(show_progress=show_progress)

    if mongodb_tools_available():
        log_message("MongoDB database tools installed via apt")
        if show_progress:
            console_msg("MongoDB database tools installed successfully.")
        return True

    log_message("MongoDB apt install completed but mongodump not found", "WARNING")
    return install_mongodb_tools_from_deb(show_progress=show_progress)


def all_required_commands_available() -> bool:
    """Quick check: are all external tools present on PATH?"""
    return all(command_exists(cmd) for cmd in list(APT_PACKAGES.keys()) + ["mongodump"])


def mark_deps_verified() -> None:
    """Record that dependency check succeeded (skip slow re-check on next launch)."""
    try:
        ensure_directories()
        DEPS_STAMP.touch()
    except OSError:
        pass


def deps_recently_verified(max_age_hours: int = 72) -> bool:
    """Return True if dependencies were verified recently."""
    try:
        if not DEPS_STAMP.exists():
            return False
        age_seconds = time.time() - DEPS_STAMP.stat().st_mtime
        return age_seconds < max_age_hours * 3600
    except OSError:
        return False


def required_commands_for_jobs() -> Tuple[Dict[str, str], bool]:
    """
    Host packages the configured jobs actually need, and whether MongoDB tools are.

    Unattended runs use this so a backup host only ever gets the clients for
    the database types it backs up: no MySQL client or third-party MongoDB apt
    repository appearing on, say, a mail server that backs up PostgreSQL.
    ArangoDB and ClickHouse need no host package (their clients run from
    Docker when not installed).
    """
    types = {job.get("database_type") for job in load_config().get("jobs", [])}
    wanted = {"python3", "gzip", "zstd"}
    if "postgresql" in types:
        wanted.add("pg_dump")
    if "mysql" in types:
        wanted.add("mysqldump")
    commands = {cmd: pkg for cmd, pkg in APT_PACKAGES.items() if cmd in wanted}
    return commands, "mongodb" in types


def install_dependencies(
    force_prompt: bool = True,
    show_progress: bool = True,
    only_for_jobs: bool = False,
) -> bool:
    """
    Detect and install missing required packages via apt.
    Requires root privileges.

    With ``only_for_jobs`` (scheduled and --run-job runs) only the clients for
    the configured jobs' database types are considered; the interactive menu
    and --install-deps still install the full set.
    """
    if only_for_jobs:
        commands, mongodb_wanted = required_commands_for_jobs()
    else:
        commands, mongodb_wanted = dict(APT_PACKAGES), True

    missing_packages: List[str] = []
    for command, package in commands.items():
        if command_exists(command):
            continue
        if package_installed(package):
            continue
        if package not in missing_packages:
            missing_packages.append(package)

    needs_mongodb = mongodb_wanted and not mongodb_tools_available()

    if not missing_packages and not needs_mongodb:
        mark_deps_verified()
        if show_progress:
            console_msg("All dependencies are already installed.")
        return True

    if os.geteuid() != 0:
        missing_display = list(missing_packages)
        if needs_mongodb:
            missing_display.append("mongodb-database-tools (MongoDB repo)")
        if force_prompt:
            msg_box(
                "Root Required",
                "Missing packages require root privileges.\n\n"
                f"Missing: {', '.join(missing_display)}\n\n"
                "Run: sudo python3 dbbackup.py",
            )
        log_message(f"Missing packages (no root): {missing_display}", "WARNING")
        return False

    log_message(
        f"Installing packages: {missing_packages}"
        + (" + MongoDB tools" if needs_mongodb else "")
    )
    if show_progress:
        console_msg("Checking and installing required packages...")
        if missing_packages:
            console_msg(f"Missing Ubuntu packages: {', '.join(missing_packages)}")
        if needs_mongodb:
            console_msg("Missing MongoDB tools: mongodb-database-tools, mongodb-mongosh")
        console_msg("Please wait — apt operations can take several minutes.")
    try:
        result = run_subprocess(
            ["apt-get", "update"],
            show_progress=show_progress,
            step="Updating apt package lists...",
        )
        if result.returncode != 0:
            raise subprocess.CalledProcessError(result.returncode, "apt-get update")
        if missing_packages:
            result = run_subprocess(
                ["apt-get", "install", "-y"] + missing_packages,
                show_progress=show_progress,
                step=f"Installing: {', '.join(missing_packages)}...",
            )
            if result.returncode != 0:
                raise subprocess.CalledProcessError(result.returncode, "apt-get install")
        if needs_mongodb and not install_mongodb_tools(show_progress=show_progress):
            raise subprocess.CalledProcessError(1, "install_mongodb_tools")
        log_message("Package installation completed")
        mark_deps_verified()
        if show_progress:
            console_msg("Dependency installation finished.")
        return True
    except subprocess.CalledProcessError as exc:
        log_exception("Package installation failed", exc)
        if force_prompt:
            msg_box("Error", f"Failed to install packages:\n{exc}")
        return False


# ---------------------------------------------------------------------------
# Lock file
# ---------------------------------------------------------------------------


def process_alive(pid: int) -> bool:
    """Return True if a process with the given PID is running."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def acquire_lock() -> bool:
    """
    Acquire exclusive lock using PID file.
    Returns False if another live backup process holds the lock.
    """
    ensure_directories()
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            pid = 0
        if process_alive(pid):
            return False
        log_message(f"Removing stale lock file (pid={pid})", "WARNING")
        try:
            LOCK_FILE.unlink()
        except OSError:
            pass

    try:
        LOCK_FILE.write_text(str(os.getpid()), encoding="utf-8")
        return True
    except OSError as exc:
        log_exception("Failed to create lock file", exc)
        return False


def release_lock() -> None:
    """Remove lock file if owned by current process."""
    if not LOCK_FILE.exists():
        return
    try:
        pid = int(LOCK_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pid = 0
    if pid == os.getpid():
        try:
            LOCK_FILE.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Database connection and discovery
# ---------------------------------------------------------------------------


def default_port(db_type: str) -> int:
    """Return default port for database type."""
    return {
        "postgresql": 5432,
        "mysql": 3306,
        "mongodb": 27017,
        "arangodb": 8529,
        "clickhouse": 9000,
    }.get(db_type, 0)


def build_connection_info(
    db_type: str,
    host: str,
    port: int,
    username: str,
    password: str,
) -> Dict[str, Any]:
    """Build a connection info dictionary."""
    return {
        "database_type": db_type,
        "host": host.strip(),
        "port": int(port),
        "username": username.strip(),
        "password": password,
    }


def job_connection(job: Dict[str, Any]) -> Dict[str, Any]:
    """Connection info for a saved job, carrying per-job client overrides."""
    conn = build_connection_info(
        job["database_type"],
        job["host"],
        job["port"],
        job["username"],
        job["password"],
    )
    if job.get("clickhouse_image"):
        conn["clickhouse_image"] = job["clickhouse_image"]
    return conn


def arango_http_get(conn: Dict[str, Any], path: str, timeout: int = 30) -> Tuple[int, str]:
    """
    Perform an authenticated GET against the ArangoDB HTTP API.

    Used for connection tests and database discovery so neither requires the
    arangosh client to be installed. Returns (status_code, body). Raises
    urllib/OSError on transport failures.
    """
    host = conn["host"]
    port = conn["port"]
    user = conn["username"]
    password = conn["password"]
    url = f"http://{host}:{port}{path}"
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    request = urllib.request.Request(url)
    request.add_header("Authorization", f"Basic {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except OSError:
            pass
        return exc.code, body


def test_connection(conn: Dict[str, Any]) -> Tuple[bool, str]:
    """Test database connectivity. Returns (success, error_message)."""
    db_type = conn["database_type"]
    host = conn["host"]
    port = conn["port"]
    user = conn["username"]
    password = conn["password"]

    env = os.environ.copy()
    try:
        if db_type == "postgresql":
            env["PGPASSWORD"] = password
            command = [
                "psql",
                "-h",
                host,
                "-p",
                str(port),
                "-U",
                user,
                "-d",
                "postgres",
                "-At",
                "-c",
                "SELECT 1;",
            ]
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                env=env,
                timeout=30,
                check=False,
            )
            if result.returncode != 0:
                err = (result.stderr or result.stdout or "Connection failed").strip()
                return False, err
            return True, ""

        if db_type == "mysql":
            env["MYSQL_PWD"] = password
            command = [
                "mysql",
                "-h",
                host,
                "-P",
                str(port),
                "-u",
                user,
                "-N",
                "-e",
                "SELECT 1;",
            ]
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                env=env,
                timeout=30,
                check=False,
            )
            if result.returncode != 0:
                err = (result.stderr or result.stdout or "Connection failed").strip()
                return False, err
            return True, ""

        if db_type == "mongodb":
            uri = (
                f"mongodb://{urllib.parse.quote(user)}:"
                f"{urllib.parse.quote(password)}@{host}:{port}/admin"
            )
            command = [
                "mongosh",
                "--quiet",
                uri,
                "--eval",
                "db.runCommand({ ping: 1 })",
            ]
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if result.returncode != 0:
                # Fallback to legacy mongo shell if mongosh unavailable
                command[0] = "mongo"
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
            if result.returncode != 0:
                err = (result.stderr or result.stdout or "Connection failed").strip()
                return False, err
            return True, ""

        if db_type == "arangodb":
            try:
                status, body = arango_http_get(conn, "/_api/version")
            except (urllib.error.URLError, OSError) as exc:
                return False, str(exc)
            if status == 200:
                return True, ""
            if status in (401, 403):
                return False, "Authentication failed (check username/password)"
            return False, f"ArangoDB returned HTTP {status}: {body.strip()[:200]}"

        if db_type == "clickhouse":
            result = clickhouse_query(conn, "SELECT 1", timeout=30)
            if result.returncode != 0:
                err = (result.stderr or result.stdout or "Connection failed").strip()
                return False, err
            return True, ""

        return False, f"Unsupported database type: {db_type}"
    except subprocess.TimeoutExpired:
        return False, "Connection timed out"
    except OSError as exc:
        return False, str(exc)


def filter_system_databases(db_type: str, databases: List[str]) -> List[str]:
    """
    Remove built-in system databases that should never be backed up.

    Applied to automatic discovery (backup-all) and to the manual selection
    list. Matching is case-insensitive and order-preserving. For database types
    with no excluded names (e.g. PostgreSQL) the list is returned unchanged.
    """
    system = SYSTEM_DATABASES.get(db_type, set())
    if not system:
        return list(databases)
    return [db for db in databases if str(db).strip().lower() not in system]


def discover_databases(conn: Dict[str, Any]) -> Tuple[bool, List[str], str]:
    """Discover databases on remote server. Returns (ok, databases, error)."""
    db_type = conn["database_type"]
    host = conn["host"]
    port = conn["port"]
    user = conn["username"]
    password = conn["password"]
    env = os.environ.copy()

    try:
        if db_type == "postgresql":
            env["PGPASSWORD"] = password
            command = [
                "psql",
                "-h",
                host,
                "-p",
                str(port),
                "-U",
                user,
                "-d",
                "postgres",
                "-At",
                "-c",
                "SELECT datname FROM pg_database "
                "WHERE datistemplate = false ORDER BY datname;",
            ]
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                env=env,
                timeout=60,
                check=False,
            )
            if result.returncode != 0:
                return False, [], (result.stderr or result.stdout).strip()
            databases = [line.strip() for line in result.stdout.splitlines() if line.strip()]
            return True, filter_system_databases(db_type, databases), ""

        if db_type == "mysql":
            env["MYSQL_PWD"] = password
            command = [
                "mysql",
                "-h",
                host,
                "-P",
                str(port),
                "-u",
                user,
                "-N",
                "-e",
                "SHOW DATABASES;",
            ]
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                env=env,
                timeout=60,
                check=False,
            )
            if result.returncode != 0:
                return False, [], (result.stderr or result.stdout).strip()
            databases = [line.strip() for line in result.stdout.splitlines() if line.strip()]
            return True, filter_system_databases(db_type, databases), ""

        if db_type == "mongodb":
            uri = (
                f"mongodb://{urllib.parse.quote(user)}:"
                f"{urllib.parse.quote(password)}@{host}:{port}/admin"
            )
            js = "JSON.stringify(db.adminCommand({ listDatabases: 1 }).databases.map(d=>d.name))"
            command = ["mongosh", "--quiet", uri, "--eval", js]
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            if result.returncode != 0:
                command[0] = "mongo"
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
            if result.returncode != 0:
                return False, [], (result.stderr or result.stdout).strip()
            raw = result.stdout.strip()
            try:
                databases = json.loads(raw)
            except json.JSONDecodeError:
                databases = [line.strip() for line in raw.splitlines() if line.strip()]
            return True, filter_system_databases(db_type, databases), ""

        if db_type == "arangodb":
            try:
                status, body = arango_http_get(conn, "/_api/database", timeout=60)
            except (urllib.error.URLError, OSError) as exc:
                return False, [], str(exc)
            if status != 200:
                return False, [], f"ArangoDB returned HTTP {status}: {body.strip()[:200]}"
            try:
                databases = json.loads(body).get("result", [])
            except json.JSONDecodeError:
                return False, [], "Could not parse ArangoDB database list"
            return True, filter_system_databases(db_type, databases), ""

        if db_type == "clickhouse":
            result = clickhouse_query(
                conn,
                "SELECT name FROM system.databases ORDER BY name FORMAT JSONEachRow",
                timeout=60,
            )
            if result.returncode != 0:
                return False, [], (result.stderr or result.stdout).strip()
            try:
                databases = [
                    json.loads(line)["name"]
                    for line in result.stdout.splitlines()
                    if line.strip()
                ]
            except (json.JSONDecodeError, KeyError):
                return False, [], "Could not parse ClickHouse database list"
            return True, filter_system_databases(db_type, databases), ""

        return False, [], f"Unsupported database type: {db_type}"
    except subprocess.TimeoutExpired:
        return False, [], "Discovery timed out"
    except OSError as exc:
        return False, [], str(exc)


# ---------------------------------------------------------------------------
# Backup helpers
# ---------------------------------------------------------------------------


def timestamp_str(when: Optional[datetime] = None) -> str:
    """Return backup filename timestamp."""
    moment = when or datetime.now()
    return moment.strftime("%Y%m%d_%H%M%S")


def iso_timestamp(when: Optional[datetime] = None) -> str:
    """Return ISO-8601 timestamp for metadata."""
    moment = when or datetime.now()
    return moment.replace(microsecond=0).isoformat()


def sanitize_name(name: str) -> str:
    """Sanitize database name for use in filenames."""
    cleaned = re.sub(r"[^\w.\-]+", "_", name.strip())
    return cleaned or "database"


def sha256_file(path: Path) -> str:
    """Calculate SHA256 hex digest of a file."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_sha256_sidecar(backup_path: Path) -> str:
    """Write sha256sum sidecar file. Returns digest."""
    digest = sha256_file(backup_path)
    sidecar = Path(f"{backup_path}.sha256")
    sidecar.write_text(f"{digest}  {backup_path.name}\n", encoding="utf-8")
    return digest


def write_metadata(
    backup_path: Path,
    database: str,
    db_type: str,
    digest: str,
    job: Dict[str, Any],
    when: Optional[datetime] = None,
) -> None:
    """Write metadata JSON sidecar beside backup file."""
    meta = {
        "job_id": job.get("id", ""),
        "job_name": job.get("name", ""),
        "database": database,
        "database_type": db_type,
        "timestamp": iso_timestamp(when),
        "size": backup_path.stat().st_size,
        "sha256": digest,
    }
    meta_path = Path(f"{backup_path}.meta.json")
    with open(meta_path, "w", encoding="utf-8") as handle:
        json.dump(meta, handle, indent=2)
        handle.write("\n")


def verify_backup(backup_path: Path, db_type: str) -> Tuple[bool, str]:
    """
    Verify backup integrity, choosing the tool from the file extension so both
    legacy gzip (.gz/.tar.gz) and current zstd (.zst/.tar.zst) backups verify.
    """
    name = backup_path.name
    if name.endswith(".tar.zst"):
        cmd = ["tar", "--zstd", "-tf", str(backup_path)]
    elif name.endswith(".ch.tar"):
        # ClickHouse: plain tar whose members are already zstd-compressed.
        return verify_clickhouse_archive(backup_path)
    elif name.endswith(".tar.gz"):
        cmd = ["tar", "-tzf", str(backup_path)]
    elif name.endswith(".zst"):
        cmd = ["zstd", "-t", str(backup_path)]
    elif name.endswith(".gz"):
        cmd = ["gzip", "-t", str(backup_path)]
    else:
        return False, f"Unknown backup format: {name}"
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            return False, (result.stderr or "verification failed").strip()
        return True, ""
    except OSError as exc:
        return False, str(exc)


def delete_backup_artifacts(backup_path: Path) -> None:
    """Remove backup file and associated sidecars."""
    for path in (
        backup_path,
        Path(f"{backup_path}.sha256"),
        Path(f"{backup_path}.meta.json"),
    ):
        try:
            if path.exists():
                path.unlink()
        except OSError as exc:
            log_exception(f"Failed to delete {path}", exc)


def human_size(num_bytes: int) -> str:
    """Convert bytes to human-readable string."""
    if num_bytes < 0:
        num_bytes = 0
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    size = float(num_bytes)
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{num_bytes} B"


def directory_size(path: Path) -> int:
    """
    Calculate disk usage of all files under a directory.

    Hard links are counted once: a dump shared by the daily and weekly
    categories occupies the disk once, so the store total must not double it.
    """
    total = 0
    if not path.exists():
        return 0
    seen = set()
    for root, _dirs, files in os.walk(path):
        for filename in files:
            file_path = Path(root) / filename
            try:
                st = file_path.stat()
            except OSError:
                continue
            key = (st.st_dev, st.st_ino)
            if key in seen:
                continue
            seen.add(key)
            total += st.st_size
    return total


def backup_categories_for_today(when: Optional[datetime] = None) -> List[str]:
    """
    Determine backup categories to create.
    Always daily; weekly on Sunday; monthly on day 1; yearly on Jan 1.
    """
    moment = when or datetime.now()
    categories = ["daily"]
    if moment.weekday() == 6:  # Sunday
        categories.append("weekly")
    if moment.day == 1:
        categories.append("monthly")
    if moment.month == 1 and moment.day == 1:
        categories.append("yearly")
    return categories


def category_directory(category: str) -> Path:
    """Map category name to filesystem path."""
    mapping = {
        "daily": DAILY_DIR,
        "weekly": WEEKLY_DIR,
        "monthly": MONTHLY_DIR,
        "yearly": YEARLY_DIR,
    }
    return mapping[category]


def job_backup_prefix(job: Dict[str, Any]) -> str:
    """Return filename prefix identifying job backups."""
    job_id = job.get("id", "job")[:8]
    host = sanitize_name(job.get("host", "host"))
    return f"{sanitize_name(job.get('name', 'job'))}_{host}_{job_id}"


def list_databases_for_job(job: Dict[str, Any]) -> List[str]:
    """Resolve database list for a job."""
    if job.get("backup_all", True):
        conn = job_connection(job)
        ok, databases, err = discover_databases(conn)
        if not ok:
            raise RuntimeError(f"Failed to discover databases: {err}")
        return databases
    return list(job.get("databases", []))


def compressor_command() -> List[str]:
    """Streaming compressor (stdin -> stdout) used for SQL dumps."""
    return ["zstd", ZSTD_THREADS, ZSTD_LEVEL, "-c"]


def docker_available() -> bool:
    """True if the docker CLI is usable on the host."""
    return command_exists("docker")


def ensure_docker_image(image: str) -> None:
    """Pull a Docker image if it is not already present locally."""
    inspect = subprocess.run(
        ["docker", "image", "inspect", image],
        capture_output=True,
        text=True,
        check=False,
    )
    if inspect.returncode == 0:
        return
    log_message(f"Pulling Docker image {image} ...")
    pull = subprocess.run(
        ["docker", "pull", image],
        capture_output=True,
        text=True,
        check=False,
    )
    if pull.returncode != 0:
        err = (pull.stderr or pull.stdout or "docker pull failed").strip()
        raise RuntimeError(f"Failed to pull Docker image {image}: {err}")


def docker_run_prefix(
    image: str,
    env: Optional[Dict[str, str]] = None,
    volumes: Optional[Dict[str, str]] = None,
    user: Optional[str] = None,
    entrypoint: Optional[str] = None,
) -> List[str]:
    """
    Build a `docker run` prefix (host network); the client args are appended.

    ``entrypoint`` runs the tool directly and bypasses the image's own
    entrypoint script — important for images like ``mongo`` whose entrypoint
    mishandles non-server commands. When set, append only the tool's arguments
    (not the tool name).
    """
    cmd = ["docker", "run", "--rm", "--network", "host"]
    if user:
        # Run as the host process's uid:gid so files written to a bind-mounted
        # dump directory are owned by us.
        cmd += ["--user", user]
    if entrypoint:
        cmd += ["--entrypoint", entrypoint]
    for key, value in (env or {}).items():
        cmd += ["-e", f"{key}={value}"]
    for host_path, container_path in (volumes or {}).items():
        cmd += ["-v", f"{host_path}:{container_path}"]
    cmd.append(image)
    return cmd


def postgres_server_major(conn: Dict[str, Any]) -> Optional[int]:
    """Return the PostgreSQL server major version, or None if it cannot be read."""
    env = os.environ.copy()
    env["PGPASSWORD"] = conn["password"]
    try:
        result = subprocess.run(
            [
                "psql", "-h", conn["host"], "-p", str(conn["port"]),
                "-U", conn["username"], "-d", "postgres", "-At",
                "-c", "SHOW server_version_num;",
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    raw = result.stdout.strip()
    if result.returncode == 0 and raw.isdigit():
        return int(raw) // 10000
    return None


def host_postgres_max_major() -> Optional[int]:
    """Return the newest installed PostgreSQL client major version on the host."""
    base = Path("/usr/lib/postgresql")
    if base.is_dir():
        majors = [int(p.name) for p in base.iterdir() if p.name.isdigit()]
        if majors:
            return max(majors)
    try:
        result = subprocess.run(
            ["pg_dump", "--version"], capture_output=True, text=True, check=False
        )
        match = re.search(r"(\d+)\.", result.stdout)
        if match:
            return int(match.group(1))
    except OSError:
        pass
    return None


def container_image_for(db_type: str, conn: Dict[str, Any]) -> str:
    """Pick the Docker image to run a client when the host tool is unsuitable."""
    if db_type == "postgresql":
        major = postgres_server_major(conn) or DEFAULT_PG_IMAGE_MAJOR
        return f"postgres:{major}"
    if db_type == "mysql":
        return MYSQL_DOCKER_IMAGE
    if db_type == "mongodb":
        return MONGO_TOOLS_DOCKER_IMAGE
    if db_type == "arangodb":
        return conn.get("arango_image") or ARANGO_DOCKER_IMAGE
    raise RuntimeError(f"No container image for database type: {db_type}")


def use_host_client(db_type: str, conn: Dict[str, Any]) -> bool:
    """
    Decide whether to use the host client binary instead of a container.

    True when the host binary exists and is version-compatible. For PostgreSQL
    the newest installed client major must be >= the server major (pg_dump
    cannot dump a newer server); other engines only require the binary to exist.
    When the host client is unsuitable the caller falls back to Docker.
    """
    binary = {
        "postgresql": "pg_dump",
        "mysql": "mysqldump",
        "mongodb": "mongodump",
        "arangodb": "arangodump",
    }.get(db_type)
    if not binary or not command_exists(binary):
        return False
    if db_type == "postgresql":
        server_major = postgres_server_major(conn)
        host_major = host_postgres_max_major()
        if (
            server_major is not None
            and host_major is not None
            and host_major < server_major
        ):
            return False
    return True


def run_stdout_dump(
    db_type: str,
    conn: Dict[str, Any],
    base_cmd: List[str],
    password_env: Optional[str],
    output_path: Path,
) -> None:
    """
    Run a dump command that writes to stdout and compress it with zstd to
    output_path. Uses the host binary when compatible, otherwise a version-
    matched Docker image (password passed via an in-container env var).
    """
    env = os.environ.copy()
    if use_host_client(db_type, conn):
        cmd = base_cmd
        if password_env:
            env[password_env] = conn["password"]
    elif docker_available():
        image = container_image_for(db_type, conn)
        ensure_docker_image(image)
        denv = {password_env: conn["password"]} if password_env else {}
        cmd = docker_run_prefix(image, env=denv, entrypoint=base_cmd[0]) + base_cmd[1:]
    else:
        raise RuntimeError(f"No host client for {db_type} and Docker is unavailable")

    with open(output_path, "wb") as outfile:
        comp = subprocess.Popen(
            compressor_command(), stdin=subprocess.PIPE, stdout=outfile
        )
        assert comp.stdin is not None
        dump = subprocess.Popen(cmd, stdout=comp.stdin, stderr=subprocess.PIPE, env=env)
        comp.stdin.close()
        dump_stderr = dump.communicate()[1]
        comp_rc = comp.wait()
        if dump.returncode != 0:
            err = (dump_stderr or b"dump failed").decode(errors="replace")
            raise RuntimeError(err.strip())
        if comp_rc != 0:
            raise RuntimeError("zstd compression failed")


def archive_compressed(source_dir: Path, output_tzst: Path) -> None:
    """Create a zstd-compressed tar of source_dir (multi-threaded)."""
    env = os.environ.copy()
    env["ZSTD_CLEVEL"] = ZSTD_LEVEL.lstrip("-")
    env["ZSTD_NBTHREADS"] = "0"
    result = subprocess.run(
        ["tar", "--zstd", "-cf", str(output_tzst), "-C", str(source_dir), "."],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if result.returncode != 0:
        err = (result.stderr or "tar archive failed").strip()
        raise RuntimeError(err)


def run_archive_dump(
    db_type: str,
    conn: Dict[str, Any],
    base_cmd_template: List[str],
    output_tzst: Path,
    image_override: Optional[str] = None,
    require_files_suffix: Optional[str] = None,
) -> None:
    """
    Run a client that writes a directory dump (mongodump/arangodump), then
    archive it as tar.zst. ``{out}`` in base_cmd_template is the output
    directory. Host binary is used when available, otherwise a Docker image with
    the dump directory bind-mounted at /dump.
    """
    with tempfile.TemporaryDirectory(prefix=f"dbbackup_{db_type}_") as temp_dir:
        dump_dir = Path(temp_dir) / "dump"
        dump_dir.mkdir(parents=True, exist_ok=True)

        if use_host_client(db_type, conn):
            cmd = [arg.format(out=str(dump_dir)) for arg in base_cmd_template]
        elif docker_available():
            image = image_override or container_image_for(db_type, conn)
            ensure_docker_image(image)
            user = f"{os.getuid()}:{os.getgid()}"
            cmd = docker_run_prefix(
                image,
                volumes={str(dump_dir): "/dump"},
                user=user,
                entrypoint=base_cmd_template[0],
            ) + [arg.format(out="/dump") for arg in base_cmd_template[1:]]
        else:
            raise RuntimeError(f"No host client for {db_type} and Docker is unavailable")

        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "dump failed").strip()
            raise RuntimeError(err)

        if require_files_suffix:
            produced = [p for p in dump_dir.rglob("*") if p.is_file()]
            if not any(p.name.endswith(require_files_suffix) for p in produced):
                raise RuntimeError(
                    f"dump produced no data for database in {db_type} job"
                )

        archive_compressed(dump_dir, output_tzst)


def run_pg_dump(conn: Dict[str, Any], database: str, output_path: Path) -> None:
    """Dump a PostgreSQL database, compressed with zstd."""
    base = [
        "pg_dump",
        "-h", conn["host"],
        "-p", str(conn["port"]),
        "-U", conn["username"],
        "-d", database,
        "--no-owner",
        "--no-privileges",
    ]
    run_stdout_dump("postgresql", conn, base, "PGPASSWORD", output_path)


def run_pg_globals(conn: Dict[str, Any], output_path: Path) -> None:
    """Dump PostgreSQL global roles, compressed with zstd."""
    base = [
        "pg_dumpall",
        "--globals-only",
        "-h", conn["host"],
        "-p", str(conn["port"]),
        "-U", conn["username"],
    ]
    run_stdout_dump("postgresql", conn, base, "PGPASSWORD", output_path)


def run_mysqldump(conn: Dict[str, Any], database: str, output_path: Path) -> None:
    """Dump a MySQL/MariaDB database, compressed with zstd."""
    base = [
        "mysqldump",
        "-h", conn["host"],
        "-P", str(conn["port"]),
        "-u", conn["username"],
        "--single-transaction",
        "--routines",
        "--triggers",
        "--events",
        "--databases",
        database,
    ]
    run_stdout_dump("mysql", conn, base, "MYSQL_PWD", output_path)


def run_mongodump(conn: Dict[str, Any], database: str, output_tzst: Path) -> None:
    """Dump a MongoDB database and archive it as tar.zst."""
    base = [
        "mongodump",
        "--host", conn["host"],
        "--port", str(conn["port"]),
        "-u", conn["username"],
        "-p", conn["password"],
        "--authenticationDatabase", "admin",
        "--db", database,
        "--out", "{out}",
    ]
    run_archive_dump("mongodb", conn, base, output_tzst)


def run_arangodump(
    conn: Dict[str, Any],
    database: str,
    output_tzst: Path,
    image: Optional[str] = None,
) -> None:
    """
    Dump an ArangoDB database and archive it as tar.zst.

    arangodump connects to the server's HTTP endpoint (tcp://host:port), so the
    backup works over the network without touching the database container. A
    native arangodump binary is used when present; otherwise it runs from the
    official ArangoDB Docker image. An empty dump (no .structure.json files) is
    treated as a failure so a misconfigured database is caught.
    """
    endpoint = f"tcp://{conn['host']}:{conn['port']}"
    base = [
        "arangodump",
        "--server.endpoint", endpoint,
        "--server.username", conn["username"],
        "--server.password", conn["password"],
        "--server.database", database,
        "--output-directory", "{out}",
        "--overwrite", "true",
    ]
    run_archive_dump(
        "arangodb",
        conn,
        base,
        output_tzst,
        image_override=image,
        require_files_suffix=".structure.json",
    )


# ---------------------------------------------------------------------------
# ClickHouse
# ---------------------------------------------------------------------------
#
# A ClickHouse backup is a plain tar (".ch.tar") of one database:
#
#   manifest.json            tables, engines, column lists, restore order
#   restore.py               self-contained restore script (see its docstring)
#   schema/000_database.sql  CREATE DATABASE
#   schema/<tier>_<n>.sql    one CREATE statement per table/view/dictionary
#   data/<n>.native.zst      rows of every table that stores data, in
#                            ClickHouse Native format, zstd-compressed
#
# Members are compressed individually, so the outer tar is not compressed
# again. Everything goes over the native protocol with clickhouse-client, so
# the backup host needs no access to the server's filesystem or a backup disk.
# Tables are exported one by one: ClickHouse has no cross-table snapshot, so
# each table is consistent on its own but tables are not frozen together.


def clickhouse_host_client() -> Optional[List[str]]:
    """Return the host clickhouse-client command, or None if not installed."""
    if command_exists("clickhouse-client"):
        return ["clickhouse-client"]
    if command_exists("clickhouse"):
        return ["clickhouse", "client"]
    return None


def clickhouse_quote_ident(name: str) -> str:
    """Quote a ClickHouse identifier with backticks."""
    return "`" + name.replace("\\", "\\\\").replace("`", "\\`") + "`"


def clickhouse_engine_has_data(engine: str) -> bool:
    """True for engines whose rows are stored by ClickHouse and must be exported."""
    return engine.endswith("MergeTree") or engine in CLICKHOUSE_DATA_ENGINES


def clickhouse_restore_tier(engine: str) -> int:
    """
    Order in which objects are recreated on restore.

    1 = tables holding data, 2 = other table engines (Distributed, Kafka, ...),
    3 = dictionaries, 4 = views, 5 = materialized views. Materialized views
    come last and are created only after the data is loaded (see restore.py),
    otherwise reloading their source tables would fire them a second time.
    """
    if engine == "MaterializedView":
        return 5
    if engine in ("View", "LiveView", "WindowView"):
        return 4
    if engine == "Dictionary":
        return 3
    if clickhouse_engine_has_data(engine):
        return 1
    return 2


class ClickHouseClient:
    """
    Run clickhouse-client against one server.

    The username and password are written to a 0600 client config file rather
    than passed on the command line, so the password never shows up in `ps` or
    in Docker container metadata. The host client is used when installed;
    otherwise the client runs from the ClickHouse Docker image with that config
    file bind-mounted read-only.
    """

    def __init__(self, conn: Dict[str, Any]) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="dbbackup_ch_")
        cfg = Path(self._tmp.name) / "client.xml"
        fd = os.open(cfg, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(
                "<config>\n"
                f"  <user>{html.escape(conn['username'])}</user>\n"
                f"  <password>{html.escape(conn['password'])}</password>\n"
                "</config>\n"
            )
        host_cmd = clickhouse_host_client()
        if host_cmd:
            base = host_cmd + ["--config-file", str(cfg)]
        elif docker_available():
            image = conn.get("clickhouse_image") or CLICKHOUSE_DOCKER_IMAGE
            ensure_docker_image(image)
            base = docker_run_prefix(
                image,
                volumes={self._tmp.name: "/dbbackup-ch:ro"},
                entrypoint="clickhouse-client",
            ) + ["--config-file", "/dbbackup-ch/client.xml"]
        else:
            self._tmp.cleanup()
            raise RuntimeError("No clickhouse-client on host and Docker is unavailable")
        self.base = base + ["--host", conn["host"], "--port", str(conn["port"])]

    def __enter__(self) -> "ClickHouseClient":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self._tmp.cleanup()

    def command(self, query: str, params: Optional[Dict[str, str]] = None) -> List[str]:
        """Build a client command; ``params`` fill {name:String} placeholders."""
        cmd = list(self.base) + ["--query", query]
        for key, value in (params or {}).items():
            cmd.append(f"--param_{key}={value}")
        return cmd

    def query(
        self,
        query: str,
        params: Optional[Dict[str, str]] = None,
        timeout: int = 300,
    ) -> subprocess.CompletedProcess:
        """Run a query and capture its text output."""
        return subprocess.run(
            self.command(query, params),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def json_rows(
        self,
        query: str,
        params: Optional[Dict[str, str]] = None,
        timeout: int = 300,
    ) -> List[Dict[str, Any]]:
        """Run a query and return its rows as dicts (raises on failure)."""
        result = self.query(f"{query} FORMAT JSONEachRow", params, timeout)
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout or "query failed").strip())
        return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


def clickhouse_query(
    conn: Dict[str, Any], query: str, timeout: int = 60
) -> subprocess.CompletedProcess:
    """One-off query used by the connection test and database discovery."""
    try:
        with ClickHouseClient(conn) as client:
            return client.query(query, timeout=timeout)
    except RuntimeError as exc:
        return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=str(exc))


def clickhouse_export_table(
    client: ClickHouseClient, query: str, output_path: Path
) -> None:
    """Stream one SELECT ... FORMAT Native through zstd into output_path."""
    with open(output_path, "wb") as outfile:
        comp = subprocess.Popen(compressor_command(), stdin=subprocess.PIPE, stdout=outfile)
        assert comp.stdin is not None
        dump = subprocess.Popen(
            client.command(query), stdout=comp.stdin, stderr=subprocess.PIPE
        )
        comp.stdin.close()
        dump_stderr = dump.communicate()[1]
        comp_rc = comp.wait()
    if dump.returncode != 0:
        err = (dump_stderr or b"export failed").decode(errors="replace")
        raise RuntimeError(err.strip())
    if comp_rc != 0:
        raise RuntimeError("zstd compression failed")
    check = subprocess.run(
        ["zstd", "-t", "-q", str(output_path)], capture_output=True, text=True, check=False
    )
    if check.returncode != 0:
        raise RuntimeError(f"zstd test failed for {output_path.name}: {check.stderr.strip()}")


CLICKHOUSE_RESTORE_SCRIPT = r'''#!/usr/bin/env python3
"""
Restore a DBBackup ClickHouse archive.

    mkdir restore && tar -xf <backup>.ch.tar -C restore
    python3 restore/restore.py [clickhouse-client options]

    e.g. python3 restore/restore.py --host 127.0.0.1 --port 9000 \
             --user default --password '...'

Needs clickhouse-client and zstd on PATH. The database is recreated under its
original name (CREATE statements reference it), so restore into a server or
database name that does not hold these tables yet.

Order: database, tables, other engines, dictionaries, views, table data,
materialized views, then the data of materialized views' inner tables.
Materialized views are created only after their source tables are loaded so
the reload does not fire them and duplicate rows in their targets.
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CLIENT = ["clickhouse-client"] + sys.argv[1:]


def q(name):
    return "`" + name.replace("\\", "\\\\").replace("`", "\\`") + "`"


def run_sql(path):
    sql = (HERE / path).read_text(encoding="utf-8").strip().rstrip(";")
    subprocess.run(CLIENT + ["--query", sql], check=True)


def load(database, table, columns, data_file):
    cols = ", ".join(q(c) for c in columns)
    query = f"INSERT INTO {q(database)}.{q(table)} ({cols}) FORMAT Native"
    dec = subprocess.Popen(["zstd", "-dc", str(HERE / data_file)], stdout=subprocess.PIPE)
    # An empty table exports zero bytes, and ClickHouse rejects an INSERT
    # without data (NO_DATA_TO_INSERT), so peek before starting the insert.
    first = dec.stdout.read(1 << 16)
    if not first:
        dec.stdout.close()
        if dec.wait() != 0:
            sys.exit(f"cannot decompress {data_file}")
        return
    ins = subprocess.Popen(CLIENT + ["--query", query], stdin=subprocess.PIPE)
    try:
        ins.stdin.write(first)
        shutil.copyfileobj(dec.stdout, ins.stdin, 1 << 20)
    except BrokenPipeError:
        pass  # the client exited early; its return code reports why
    finally:
        ins.stdin.close()
        dec.stdout.close()
    if dec.wait() != 0 or ins.wait() != 0:
        sys.exit(f"failed to load {database}.{table} from {data_file}")


def inner_table(database, view):
    query = (
        "SELECT name FROM system.tables WHERE database = {db:String} AND ("
        "name = concat('.inner_id.', toString((SELECT uuid FROM system.tables "
        "WHERE database = {db:String} AND name = {mv:String}))) "
        "OR name = concat('.inner.', {mv:String}))"
    )
    out = subprocess.run(
        CLIENT + ["--query", query, f"--param_db={database}", f"--param_mv={view}"],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    if len(out) != 1:
        sys.exit(f"cannot find the inner table of materialized view {view}")
    return out[0]


def main():
    manifest = json.loads((HERE / "manifest.json").read_text(encoding="utf-8"))
    database = manifest["database"]
    tables = manifest["tables"]
    run_sql("schema/000_database.sql")
    for tier in (1, 2, 3, 4):
        for t in tables:
            if t.get("tier") == tier:
                run_sql(t["schema_file"])
    for t in tables:
        if t.get("data_file") and not t.get("inner_of"):
            load(database, t["name"], t["columns"], t["data_file"])
    for t in tables:
        if t.get("tier") == 5:
            run_sql(t["schema_file"])
    for t in tables:
        if t.get("inner_of"):
            load(database, inner_table(database, t["inner_of"]), t["columns"], t["data_file"])
    print(f"restored {database}: {len(tables)} objects")


if __name__ == "__main__":
    main()
'''


def run_clickhouse_dump(conn: Dict[str, Any], database: str, output_tar: Path) -> None:
    """
    Dump one ClickHouse database into a .ch.tar archive (layout above).

    Work happens in a hidden staging directory beside the final archive (same
    filesystem, so no /tmp size limit) and tar --remove-files drops each member
    once archived, keeping peak usage close to one copy of the dump.
    """
    staging = output_tar.parent / f".staging_{output_tar.name}"
    if staging.exists():
        shutil.rmtree(staging)
    (staging / "schema").mkdir(parents=True)
    (staging / "data").mkdir()
    try:
        with ClickHouseClient(conn) as ch:
            params = {"db": database}
            server_version = ch.json_rows("SELECT version() AS v")[0]["v"]
            db_stmt = ch.json_rows(
                f"SHOW CREATE DATABASE {clickhouse_quote_ident(database)}"
            )[0]["statement"]
            tables = ch.json_rows(
                "SELECT name, engine, create_table_query, toString(uuid) AS uuid, "
                "total_rows FROM system.tables "
                "WHERE database = {db:String} AND NOT is_temporary ORDER BY name",
                params,
            )

            # Inner tables of materialized views (".inner_id.<uuid>" in Atomic
            # databases, ".inner.<view>" in Ordinary ones) are created by the
            # view itself, so only their data is kept, tied to the view name.
            inner_owner: Dict[str, str] = {}
            for t in tables:
                if t["engine"] == "MaterializedView":
                    inner_owner[f".inner_id.{t['uuid']}"] = t["name"]
                    inner_owner[f".inner.{t['name']}"] = t["name"]

            (staging / "schema" / "000_database.sql").write_text(
                re.sub(r"^CREATE DATABASE ", "CREATE DATABASE IF NOT EXISTS ", db_stmt, count=1)
                + ";\n",
                encoding="utf-8",
            )

            entries: List[Dict[str, Any]] = []
            ordered = sorted(
                tables, key=lambda r: (clickhouse_restore_tier(r["engine"]), r["name"])
            )
            for seq, t in enumerate(ordered, start=1):
                name, engine = t["name"], t["engine"]
                entry: Dict[str, Any] = {
                    "name": name,
                    "engine": engine,
                    "total_rows": t.get("total_rows"),
                }
                if name in inner_owner:
                    entry["inner_of"] = inner_owner[name]
                else:
                    tier = clickhouse_restore_tier(engine)
                    schema_file = f"schema/{tier}_{seq:04d}.sql"
                    (staging / schema_file).write_text(
                        t["create_table_query"].rstrip().rstrip(";") + ";\n",
                        encoding="utf-8",
                    )
                    entry["tier"] = tier
                    entry["schema_file"] = schema_file

                if clickhouse_engine_has_data(engine):
                    # Only stored columns: ALIAS/MATERIALIZED/EPHEMERAL ones are
                    # recomputed by the server on insert.
                    columns = [
                        c["name"]
                        for c in ch.json_rows(
                            "SELECT name FROM system.columns "
                            "WHERE database = {db:String} AND table = {t:String} "
                            "AND default_kind IN ('', 'DEFAULT') ORDER BY position",
                            {"db": database, "t": name},
                        )
                    ]
                    data_file = f"data/{seq:04d}.native.zst"
                    select = (
                        "SELECT "
                        + ", ".join(clickhouse_quote_ident(c) for c in columns)
                        + f" FROM {clickhouse_quote_ident(database)}."
                        + f"{clickhouse_quote_ident(name)} FORMAT Native"
                    )
                    clickhouse_export_table(ch, select, staging / data_file)
                    entry["columns"] = columns
                    entry["data_file"] = data_file
                entries.append(entry)

        manifest = {
            "format": "dbbackup-clickhouse/1",
            "database": database,
            "server_version": server_version,
            "created": iso_timestamp(),
            "tables": entries,
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        restore = staging / "restore.py"
        restore.write_text(CLICKHOUSE_RESTORE_SCRIPT, encoding="utf-8")
        os.chmod(restore, 0o755)

        result = subprocess.run(
            ["tar", "-cf", str(output_tar), "--remove-files", "-C", str(staging),
             "manifest.json", "restore.py", "schema", "data"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError((result.stderr or "tar archive failed").strip())
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def verify_clickhouse_archive(backup_path: Path) -> Tuple[bool, str]:
    """A ClickHouse archive must list cleanly and carry its manifest and restore script."""
    result = subprocess.run(
        ["tar", "-tf", str(backup_path)], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        return False, (result.stderr or "tar listing failed").strip()
    members = set(result.stdout.split())
    missing = {"manifest.json", "restore.py"} - members
    if missing:
        return False, f"archive is missing {', '.join(sorted(missing))}"
    return True, ""


def backup_extension(db_type: str) -> str:
    """Return backup file extension for database type."""
    if db_type in ("mongodb", "arangodb"):
        return "tar.zst"
    if db_type == "clickhouse":
        return "ch.tar"
    return "sql.zst"


def min_free_bytes() -> int:
    """Free-space floor for the backup filesystem (config "min_free_gb")."""
    try:
        gb = float(load_config().get("min_free_gb", DEFAULT_MIN_FREE_GB))
    except (TypeError, ValueError):
        gb = DEFAULT_MIN_FREE_GB
    return int(max(gb, 0) * 1024 ** 3)


def ensure_free_space(target_dir: Path) -> None:
    """Raise before dumping when the backup filesystem is below the floor."""
    floor = min_free_bytes()
    free = shutil.disk_usage(target_dir).free
    if free < floor:
        raise RuntimeError(
            f"Not enough free space on {target_dir}: {human_size(free)} free, "
            f"need at least {human_size(floor)} (config min_free_gb)"
        )


def link_or_copy(source: Path, dest: Path) -> None:
    """Hard-link source to dest (same filesystem); fall back to a copy."""
    try:
        os.link(source, dest)
    except OSError:
        shutil.copy2(source, dest)


def reuse_backup(
    job: Dict[str, Any],
    database: str,
    db_type: str,
    source: Path,
    backup_path: Path,
    moment: datetime,
) -> int:
    """
    Publish an already-verified dump from this run under another category.

    On days that are daily+weekly (+monthly...) the database is dumped once and
    the other categories get a hard link, instead of re-running the dump
    against the server. Retention deletes one link at a time, so each category
    still keeps its own copy count. Returns the size of the published file.
    """
    link_or_copy(source, backup_path)
    # Same bytes as the source, whose digest was computed right after its dump;
    # re-hashing a multi-hundred-GB file here would only cost another full read.
    try:
        digest = Path(f"{source}.sha256").read_text(encoding="utf-8").split()[0]
    except (OSError, IndexError):
        digest = sha256_file(backup_path)
    Path(f"{backup_path}.sha256").write_text(
        f"{digest}  {backup_path.name}\n", encoding="utf-8"
    )
    write_metadata(backup_path, database, db_type, digest, job, moment)
    return backup_path.stat().st_size


def backup_one_database(
    job: Dict[str, Any],
    database: str,
    category: str,
    when: Optional[datetime] = None,
    reuse_from: Optional[Path] = None,
) -> Tuple[bool, str, int, float, Optional[Path]]:
    """
    Backup a single database for one category.

    With ``reuse_from`` (a dump of the same database made earlier in this run)
    the file is linked instead of dumped again.
    Returns (success, error_message, size_bytes, duration_seconds, path).
    """
    moment = when or datetime.now()
    conn = job_connection(job)
    db_type = job["database_type"]
    ext = backup_extension(db_type)
    ts = timestamp_str(moment)
    safe_db = sanitize_name(database)
    prefix = job_backup_prefix(job)
    target_dir = category_directory(category)
    target_dir.mkdir(parents=True, exist_ok=True)
    backup_path = target_dir / f"{prefix}_{safe_db}_{ts}.{ext}"

    start = time.time()
    job_name = job.get("name", "unnamed")

    if reuse_from is not None:
        try:
            size_bytes = reuse_backup(job, database, db_type, reuse_from, backup_path, moment)
            duration = time.time() - start
            log_message(
                f"Backup reused | job={job_name} | db={database} | category={category} | "
                f"from={reuse_from.parent.name}/{reuse_from.name} | file={backup_path.name}"
            )
            return True, "", size_bytes, duration, backup_path
        except Exception as exc:
            # Fall through to a fresh dump rather than lose this category.
            if backup_path.exists():
                delete_backup_artifacts(backup_path)
            log_exception(
                f"Backup reuse failed, dumping again | job={job_name} | db={database}", exc
            )
            start = time.time()

    log_message(
        f"Starting backup | job={job_name} | db={database} | "
        f"category={category} | host={job.get('host')}"
    )

    try:
        ensure_free_space(target_dir)
        if db_type == "postgresql":
            run_pg_dump(conn, database, backup_path)
        elif db_type == "mysql":
            run_mysqldump(conn, database, backup_path)
        elif db_type == "mongodb":
            run_mongodump(conn, database, backup_path)
        elif db_type == "arangodb":
            run_arangodump(conn, database, backup_path, job.get("arango_image"))
        elif db_type == "clickhouse":
            run_clickhouse_dump(conn, database, backup_path)
        else:
            raise RuntimeError(f"Unsupported database type: {db_type}")

        ok, verr = verify_backup(backup_path, db_type)
        if not ok:
            delete_backup_artifacts(backup_path)
            duration = time.time() - start
            log_message(
                f"Backup verification failed | job={job_name} | db={database} | "
                f"error={verr}",
                "ERROR",
            )
            send_telegram_failure(job, database, verr)
            return False, verr, 0, duration, None

        digest = write_sha256_sidecar(backup_path)
        write_metadata(backup_path, database, db_type, digest, job, moment)
        size_bytes = backup_path.stat().st_size
        duration = time.time() - start
        log_message(
            f"Backup completed | job={job_name} | db={database} | "
            f"category={category} | duration={duration:.2f}s | "
            f"size={size_bytes} | file={backup_path.name}"
        )
        send_telegram_success(job, database, duration, size_bytes)
        return True, "", size_bytes, duration, backup_path
    except Exception as exc:
        duration = time.time() - start
        if backup_path.exists():
            delete_backup_artifacts(backup_path)
        err = str(exc)
        log_exception(
            f"Backup failed | job={job_name} | db={database} | category={category}",
            exc,
        )
        send_telegram_failure(job, database, err)
        return False, err, 0, duration, None


def backup_postgresql_globals(
    job: Dict[str, Any],
    category: str,
    when: Optional[datetime] = None,
    reuse_from: Optional[Path] = None,
) -> Tuple[bool, str, int, float, Optional[Path]]:
    """Backup PostgreSQL global roles separately (linked when ``reuse_from``)."""
    moment = when or datetime.now()
    conn = job_connection(job)
    ts = timestamp_str(moment)
    prefix = job_backup_prefix(job)
    target_dir = category_directory(category)
    target_dir.mkdir(parents=True, exist_ok=True)
    backup_path = target_dir / f"globals_{prefix}_{ts}.sql.zst"
    start = time.time()
    job_name = job.get("name", "unnamed")
    database = "globals"

    if reuse_from is not None:
        try:
            size_bytes = reuse_backup(
                job, database, "postgresql", reuse_from, backup_path, moment
            )
            return True, "", size_bytes, time.time() - start, backup_path
        except Exception as exc:
            if backup_path.exists():
                delete_backup_artifacts(backup_path)
            log_exception(f"Globals reuse failed, dumping again | job={job_name}", exc)

    try:
        ensure_free_space(target_dir)
        run_pg_globals(conn, backup_path)
        ok, verr = verify_backup(backup_path, "postgresql")
        if not ok:
            delete_backup_artifacts(backup_path)
            duration = time.time() - start
            send_telegram_failure(job, database, verr)
            return False, verr, 0, duration, None
        digest = write_sha256_sidecar(backup_path)
        write_metadata(backup_path, database, "postgresql", digest, job, moment)
        size_bytes = backup_path.stat().st_size
        duration = time.time() - start
        log_message(
            f"Globals backup completed | job={job_name} | duration={duration:.2f}s | "
            f"size={size_bytes}"
        )
        send_telegram_success(job, database, duration, size_bytes)
        return True, "", size_bytes, duration, backup_path
    except Exception as exc:
        duration = time.time() - start
        if backup_path.exists():
            delete_backup_artifacts(backup_path)
        err = str(exc)
        log_exception(f"Globals backup failed | job={job_name}", exc)
        send_telegram_failure(job, database, err)
        return False, err, 0, duration, None


def backup_path_from_meta(meta_path: Path) -> Path:
    """Resolve backup file path from its .meta.json sidecar path."""
    name = meta_path.name
    if name.endswith(".meta.json"):
        backup_name = name[: -len(".meta.json")]
        return meta_path.with_name(backup_name)
    return meta_path


def retention_count(
    retention: Dict[str, Any], category: str, default: Optional[int] = None
) -> int:
    """
    Resolve how many copies to keep for a category.

    Reads new-style count keys (e.g. ``daily_count``). For jobs created before
    the switch to count-based retention, the legacy time-based keys
    (``daily_days``/``weekly_weeks``/``monthly_months``) are reinterpreted as
    copy counts so those jobs keep working. Yearly has no legacy key. When a
    category is absent entirely, the default falls back to DEFAULT_RETENTION.
    """
    if default is None:
        default = DEFAULT_RETENTION.get(f"{category}_count", 3)
    new_key = f"{category}_count"
    if new_key in retention:
        try:
            return max(0, int(retention[new_key]))
        except (TypeError, ValueError):
            return default
    legacy_key = {
        "daily": "daily_days",
        "weekly": "weekly_weeks",
        "monthly": "monthly_months",
    }.get(category)
    if legacy_key and legacy_key in retention:
        try:
            return max(0, int(retention[legacy_key]))
        except (TypeError, ValueError):
            return default
    return default


def apply_retention(job: Dict[str, Any]) -> None:
    """
    Enforce per-category copy counts for a job.

    Retention is count-based: for each category (daily/weekly/monthly/yearly)
    the newest N backups of every database are kept and older copies deleted.
    Each database — and the PostgreSQL globals dump — is counted independently,
    so a job with several databases keeps N copies of each, not N in total.
    """
    retention = job.get("retention", {})
    job_id = job.get("id", "")

    for category in ("daily", "weekly", "monthly", "yearly"):
        keep_count = retention_count(retention, category)
        target_dir = category_directory(category)
        if not target_dir.exists():
            continue

        # Group this job's backups in the category by database (logical target).
        groups: Dict[str, List[Tuple[datetime, Path]]] = {}
        for meta_path in target_dir.glob("*.meta.json"):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if meta.get("job_id") != job_id:
                continue
            database = str(meta.get("database", ""))
            timestamp_raw = meta.get("timestamp", "")
            try:
                sort_key = datetime.fromisoformat(timestamp_raw)
            except ValueError:
                try:
                    sort_key = datetime.fromtimestamp(meta_path.stat().st_mtime)
                except OSError:
                    sort_key = datetime.min
            groups.setdefault(database, []).append((sort_key, meta_path))

        for database, entries in groups.items():
            # Newest first; keep the first keep_count, delete the remainder.
            entries.sort(key=lambda item: item[0], reverse=True)
            for _sort_key, meta_path in entries[keep_count:]:
                backup_path = backup_path_from_meta(meta_path)
                log_message(
                    f"Retention delete | job={job.get('name')} | db={database} | "
                    f"file={backup_path.name} | category={category} | "
                    f"keep={keep_count}"
                )
                delete_backup_artifacts(backup_path)


def run_job_backups(
    job: Dict[str, Any],
    when: Optional[datetime] = None,
    categories: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Execute all backups for a single job."""
    moment = when or datetime.now()
    cats = categories or backup_categories_for_today(moment)
    summary = {
        "job": job.get("name", "unnamed"),
        "database_type": job.get("database_type", ""),
        "host": job.get("host", ""),
        "port": job.get("port", ""),
        "success": 0,
        "failed": 0,
        "total_size": 0,
        "errors": [],
        "results": [],
    }
    log_message(
        f"Job run start | job={summary['job']} | categories={','.join(cats)} | "
        f"start={iso_timestamp(moment)}"
    )

    try:
        databases = list_databases_for_job(job)
    except Exception as exc:
        err = str(exc)
        summary["errors"].append(err)
        summary["failed"] += 1
        summary["results"].append(
            {"category": "", "database": "(discovery)", "ok": False, "size": 0,
             "duration": 0, "error": err}
        )
        log_exception(f"Job discovery failed | job={summary['job']}", exc)
        send_telegram_failure(job, "discovery", err)
        return summary

    if not databases and job.get("database_type") != "postgresql":
        err = "No databases selected or discovered"
        summary["errors"].append(err)
        summary["failed"] += 1
        summary["results"].append(
            {"category": "", "database": "(all)", "ok": False, "size": 0,
             "duration": 0, "error": err}
        )
        send_telegram_failure(job, "all", err)
        return summary

    # Each database is dumped once per run; further categories of the same run
    # (weekly on Sunday, monthly on the 1st...) reuse that dump via a hard link.
    # Re-dumping a large production database two or three times in one night
    # only adds load to the server. Reused copies do not count toward
    # total_size, which reports data actually transferred and written.
    # Keyed by (is_globals, name) so a database literally named "globals" does
    # not collide with the PostgreSQL globals dump.
    first_dump: Dict[Tuple[bool, str], Path] = {}

    def record(
        category: str, database: str, key: Tuple[bool, str], outcome: Tuple[Any, ...]
    ) -> None:
        ok, err, size, duration, path = outcome
        reused = ok and key in first_dump
        if ok and path is not None and key not in first_dump:
            first_dump[key] = path
        summary["results"].append(
            {"category": category, "database": database, "ok": ok,
             "size": size, "duration": duration, "error": err, "reused": reused}
        )
        if ok:
            summary["success"] += 1
            if not reused:
                summary["total_size"] += size
        else:
            summary["failed"] += 1
            summary["errors"].append(err)

    for category in cats:
        if job.get("database_type") == "postgresql":
            key = (True, "globals")
            record(category, "globals", key, backup_postgresql_globals(
                job, category, moment, reuse_from=first_dump.get(key)
            ))

        for database in databases:
            key = (False, database)
            record(category, database, key, backup_one_database(
                job, database, category, moment, reuse_from=first_dump.get(key)
            ))

    apply_retention(job)
    end = datetime.now()
    log_message(
        f"Job run end | job={summary['job']} | end={iso_timestamp(end)} | "
        f"success={summary['success']} | failed={summary['failed']} | "
        f"total_size={summary['total_size']}"
    )
    return summary


def run_all_backups(when: Optional[datetime] = None) -> int:
    """
    Run all configured backup jobs.
    Returns process exit code (0 success, non-zero if any failure).
    """
    if not acquire_lock():
        message = "Backup already running."
        log_message(message, "WARNING")
        print(message, file=sys.stderr)
        return 1

    start = datetime.now()
    log_message(f"Scheduled run start | start={iso_timestamp(start)}")

    try:
        config = load_config()
        if not config.get("jobs"):
            log_message("No backup jobs configured", "WARNING")
            return 0

        any_failed = False
        total_size = 0
        summaries = []
        for job in config["jobs"]:
            result = run_job_backups(job, when)
            summaries.append(result)
            total_size += result["total_size"]
            if result["failed"] > 0:
                any_failed = True

        end = datetime.now()
        send_telegram_run_report(summaries, start, end, total_size)
        duration = (end - start).total_seconds()
        log_message(
            f"Scheduled run end | end={iso_timestamp(end)} | "
            f"duration={duration:.2f}s | total_size={total_size}"
        )
        return 1 if any_failed else 0
    finally:
        release_lock()


# ---------------------------------------------------------------------------
# Telegram integration
# ---------------------------------------------------------------------------


def send_telegram_message(
    text: str, config: Optional[Dict[str, Any]] = None, silent: bool = False
) -> Tuple[bool, str]:
    """
    Send a message via Telegram Bot API.

    When ``silent`` is True the message is delivered without a sound or
    vibration (Telegram's ``disable_notification``).
    """
    cfg = config or load_config()
    telegram = cfg.get("telegram", {})
    if not telegram.get("enabled"):
        return False, "Telegram disabled"
    token = telegram.get("bot_token", "").strip()
    chat_id = telegram.get("chat_id", "").strip()
    if not token or not chat_id:
        return False, "Telegram not configured"

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    fields = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }
    if silent:
        fields["disable_notification"] = "true"
    payload = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(url, data=payload, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
        data = json.loads(body)
        if data.get("ok"):
            return True, ""
        return False, data.get("description", "Unknown Telegram error")
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
        return False, str(exc)


def send_telegram_success(
    job: Dict[str, Any],
    database: str,
    duration: float,
    size_bytes: int,
) -> None:
    """Send a per-database success notification (only in per-database mode)."""
    config = load_config()
    telegram = config.get("telegram", {})
    if not telegram.get("enabled") or not telegram.get("per_database_notifications", False):
        return
    server = html.escape(f"{job.get('host')}:{job.get('port')}")
    text = (
        "<b>Backup Success</b>\n"
        f"Server: {server}\n"
        f"Database: {html.escape(str(database))}\n"
        f"Duration: {duration:.2f}s\n"
        f"Backup Size: {human_size(size_bytes)} ({size_bytes} bytes)"
    )
    silent = bool(config.get("telegram", {}).get("silent_success", False))
    ok, err = send_telegram_message(text, config, silent=silent)
    if not ok:
        log_message(f"Telegram success notification failed: {err}", "WARNING")


def send_telegram_failure(
    job: Dict[str, Any],
    database: str,
    error: str,
) -> None:
    """Send a per-database failure notification (only in per-database mode)."""
    config = load_config()
    telegram = config.get("telegram", {})
    if not telegram.get("enabled") or not telegram.get("per_database_notifications", False):
        return
    server = html.escape(f"{job.get('host')}:{job.get('port')}")
    text = (
        "<b>Backup Failed</b>\n"
        f"Server: {server}\n"
        f"Database: {html.escape(str(database))}\n"
        f"Error: {html.escape(str(error))}"
    )
    silent = bool(config.get("telegram", {}).get("silent_failure", False))
    ok, err = send_telegram_message(text, config, silent=silent)
    if not ok:
        log_message(f"Telegram failure notification failed: {err}", "WARNING")


def human_duration(seconds: float) -> str:
    """Compact human-readable duration (e.g. '45s', '3m 12s', '1h 29m')."""
    total = int(round(max(0.0, seconds)))
    if total < 60:
        return f"{total}s"
    minutes, sec = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {sec}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def format_run_report(
    summaries: List[Dict[str, Any]],
    start: datetime,
    end: datetime,
    total_size: int,
) -> str:
    """
    Build the consolidated run report. The per-database table is wrapped in a
    <pre> block (monospace) with space-padded columns so database names and
    sizes line up — Telegram's normal font is proportional and won't align.
    """
    esc = html.escape
    cats = sorted(
        {r.get("category", "") for s in summaries for r in s.get("results", []) if r.get("category")}
    )
    cat_label = ", ".join(c.capitalize() for c in cats) or "Backup"
    elapsed = human_duration((end - start).total_seconds())

    # First pass: dedupe per job, collect rows, measure column widths.
    blocks = []  # (header, [(ok, db, detail)])
    total_ok = total_fail = 0
    name_w = 0
    size_w = 0
    for s in summaries:
        by_db: Dict[str, Dict[str, Any]] = {}
        for r in s.get("results", []):
            db = str(r.get("database", ""))
            if db not in by_db or not r.get("ok", False):  # prefer a failed entry
                by_db[db] = r
        if not by_db:
            continue
        rows = []
        job_failed = any(not r.get("ok", False) for r in by_db.values())
        for db, r in by_db.items():
            short = db if len(db) <= 22 else db[:21] + "…"
            if r.get("ok", False):
                total_ok += 1
                detail = human_size(r.get("size", 0))
                rows.append((True, short, detail))
                name_w = max(name_w, len(short))
                size_w = max(size_w, len(detail))
            else:
                total_fail += 1
                rows.append((False, short, (str(r.get("error", "")) or "failed")[:44]))
        dbtype = DB_TYPES.get(s.get("database_type", ""), str(s.get("database_type", "")))
        server_icon = "⚠️" if job_failed else "🖥"
        blocks.append(
            (f"{server_icon} {s.get('job','')} · {dbtype} · {s.get('host','')}", rows)
        )

    # Second pass: render aligned monospace lines.
    table = []
    compact = []
    for i, (header, rows) in enumerate(blocks):
        if i:
            table.append("")
        table.append(header)
        ok_n = fail_n = 0
        for ok, db, detail in rows:
            if ok:
                ok_n += 1
                table.append(f"  ✅ {db.ljust(name_w)}  {detail.rjust(size_w)}")
            else:
                fail_n += 1
                table.append(f"  ❌ {db}  {detail}")
        compact.append(f"{header.split(' · ')[0]}  — {ok_n} ok · {fail_n} fail")

    daily = directory_size(DAILY_DIR)
    weekly = directory_size(WEEKLY_DIR)
    monthly = directory_size(MONTHLY_DIR)
    yearly = directory_size(YEARLY_DIR)
    store_total = directory_size(BACKUP_ROOT)
    status = "✅" if total_fail == 0 else "❌"
    footer = [
        "",
        "━" * 26,
        f"{status} Thành công {total_ok}   ❌ Lỗi {total_fail}",
        f"📦 {human_size(total_size)}   ⏱ {elapsed}",
        f"💾 D {human_size(daily)} · W {human_size(weekly)} · "
        f"M {human_size(monthly)} · Y {human_size(yearly)} · Σ {human_size(store_total)}",
    ]

    title = [
        f"<b>🗄️ DBBackup — {esc(cat_label)}</b>",
        f"🕑 {start:%d/%m %H:%M} → {end:%H:%M} · {elapsed}",
    ]

    pre_body = "\n".join(table + footer)
    text = "\n".join(title) + "\n<pre>" + esc(pre_body) + "</pre>"
    if len(text) <= 4000:
        return text
    # Too long — fall back to a compact per-job list inside the <pre>.
    pre_body = "\n".join(compact + footer)
    return "\n".join(title) + "\n<pre>" + esc(pre_body) + "</pre>"


def send_telegram_run_report(
    summaries: List[Dict[str, Any]],
    start: datetime,
    end: datetime,
    total_size: int,
) -> None:
    """Send the single consolidated run report (silent if all-ok, rings on failure)."""
    config = load_config()
    telegram = config.get("telegram", {})
    if not telegram.get("enabled"):
        return
    if not any(s.get("results") for s in summaries):
        return  # nothing was attempted
    any_fail = any(s.get("failed", 0) for s in summaries)
    silent_key = "silent_failure" if any_fail else "silent_success"
    silent = bool(telegram.get(silent_key, False))
    text = format_run_report(summaries, start, end, total_size)
    ok, err = send_telegram_message(text, config, silent=silent)
    if not ok:
        log_message(f"Telegram run report failed: {err}", "WARNING")


def telegram_configure() -> None:
    """Prompt for bot token + chat ID, validate with a test message, save."""
    config = load_config()
    telegram = config.setdefault("telegram", DEFAULT_CONFIG["telegram"].copy())

    if telegram.get("bot_token") and telegram.get("chat_id"):
        reuse = yes_no(
            "Telegram Settings",
            "Configuration found.\n\nReuse existing configuration?",
            default="yes",
        )
        if reuse:
            ok, err = send_telegram_message("DBBackup: Telegram test message successful.")
            if ok:
                telegram["enabled"] = True
                save_config(config)
                msg_box("Telegram", "Existing configuration validated and enabled.")
            else:
                msg_box("Telegram", f"Test message failed:\n{err}")
            return

    token = input_box("Telegram", "Enter Bot Token:")
    if token is None:
        return
    chat_id = input_box("Telegram", "Enter Chat ID:")
    if chat_id is None:
        return

    test_config = config.copy()
    test_config["telegram"] = {
        "enabled": True,
        "bot_token": token.strip(),
        "chat_id": chat_id.strip(),
    }
    ok, err = send_telegram_message(
        "DBBackup: Telegram test message successful.",
        test_config,
    )
    if not ok:
        msg_box("Telegram", f"Test message failed:\n{err}\n\nConfiguration not saved.")
        return

    telegram["enabled"] = True
    telegram["bot_token"] = token.strip()
    telegram["chat_id"] = chat_id.strip()
    save_config(config)
    msg_box("Telegram", "Telegram configuration saved successfully.")


def telegram_sound_settings() -> None:
    """Choose whether success/summary and failure notifications make a sound."""
    config = load_config()
    telegram = config.setdefault("telegram", DEFAULT_CONFIG["telegram"].copy())

    success = radiolist(
        "Success Notifications",
        "Sound for success and daily-summary messages:",
        [
            ("ring", "Ring (with sound)", not telegram.get("silent_success", False)),
            ("silent", "Silent (no sound) — recommended", telegram.get("silent_success", False)),
        ],
    )
    if success is None:
        return
    failure = radiolist(
        "Failure Notifications",
        "Sound for failure messages:",
        [
            ("ring", "Ring (with sound) — recommended", not telegram.get("silent_failure", False)),
            ("silent", "Silent (no sound)", telegram.get("silent_failure", False)),
        ],
    )
    if failure is None:
        return

    telegram["silent_success"] = success == "silent"
    telegram["silent_failure"] = failure == "silent"
    save_config(config)
    msg_box(
        "Telegram",
        "Notification sound saved:\n"
        f"Success / summary: {'Silent' if telegram['silent_success'] else 'Ring'}\n"
        f"Failure: {'Silent' if telegram['silent_failure'] else 'Ring'}",
    )


def menu_telegram_settings() -> None:
    """Telegram settings sub-menu: bot configuration, notification sound, test."""
    while True:
        choice = menu(
            "Telegram Settings",
            [
                ("config", "Configure bot token & chat ID"),
                ("sound", "Notification sound (ring / silent)"),
                ("test", "Send test message"),
                ("back", "Back to main menu"),
            ],
            height=14,
        )
        if not choice or choice == "back":
            return
        if choice == "config":
            telegram_configure()
        elif choice == "sound":
            telegram_sound_settings()
        elif choice == "test":
            ok, err = send_telegram_message("DBBackup: Telegram test message.")
            msg_box("Telegram", "Test message sent." if ok else f"Test failed:\n{err}")


# ---------------------------------------------------------------------------
# Job wizard and management
# ---------------------------------------------------------------------------


def prompt_connection(existing: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Prompt for database connection details."""
    db_type = existing.get("database_type") if existing else None
    if not db_type:
        choice = menu(
            "Database Type",
            [
                ("postgresql", "PostgreSQL"),
                ("mysql", "MySQL/MariaDB"),
                ("mongodb", "MongoDB"),
                ("arangodb", "ArangoDB"),
                ("clickhouse", "ClickHouse (native protocol)"),
            ],
        )
        if not choice:
            return None
        db_type = choice

    host_default = existing.get("host", "localhost") if existing else "localhost"
    port_default = str(existing.get("port", default_port(db_type))) if existing else str(
        default_port(db_type)
    )
    user_default = existing.get("username", "") if existing else ""

    host = input_box("Connection", "Host:", host_default)
    if host is None:
        return None
    port_str = input_box("Connection", "Port:", port_default)
    if port_str is None:
        return None
    try:
        port = int(port_str.strip())
    except ValueError:
        msg_box("Error", "Invalid port number.")
        return None
    username = input_box("Connection", "Username:", user_default)
    if username is None:
        return None
    password = input_box("Connection", "Password:", password=True)
    if password is None:
        return None

    return build_connection_info(db_type, host, port, username, password)


def prompt_retention(existing: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, int]]:
    """Prompt for retention settings (number of copies kept per category)."""
    current = existing.get("retention", {}) if existing else {}
    daily_default = str(retention_count(current, "daily"))
    weekly_default = str(retention_count(current, "weekly"))
    monthly_default = str(retention_count(current, "monthly"))
    yearly_default = str(retention_count(current, "yearly"))

    daily_str = input_box("Retention", "Daily copies to keep:", daily_default)
    if daily_str is None:
        return None
    weekly_str = input_box("Retention", "Weekly copies to keep:", weekly_default)
    if weekly_str is None:
        return None
    monthly_str = input_box("Retention", "Monthly copies to keep:", monthly_default)
    if monthly_str is None:
        return None
    yearly_str = input_box("Retention", "Yearly copies to keep:", yearly_default)
    if yearly_str is None:
        return None

    try:
        return {
            "daily_count": int(daily_str.strip()),
            "weekly_count": int(weekly_str.strip()),
            "monthly_count": int(monthly_str.strip()),
            "yearly_count": int(yearly_str.strip()),
        }
    except ValueError:
        msg_box("Error", "Retention values must be integers.")
        return None


def prompt_database_selection(
    conn: Dict[str, Any],
    existing: Optional[Dict[str, Any]] = None,
) -> Optional[Tuple[bool, List[str]]]:
    """Prompt for all/manual database selection."""
    ok, databases, err = discover_databases(conn)
    if not ok:
        msg_box("Discovery Failed", f"Could not list databases:\n{err}")
        return None
    if not databases:
        msg_box("Discovery", "No databases found on server.")
        return None

    default_mode = "all"
    if existing:
        default_mode = "all" if existing.get("backup_all", True) else "manual"

    mode = radiolist(
        "Database Selection",
        "Choose backup mode:",
        [
            ("all", "Backup ALL databases automatically", default_mode == "all"),
            ("manual", "Select databases manually", default_mode == "manual"),
        ],
    )
    if not mode:
        return None

    if mode == "all":
        return True, []

    existing_dbs = set(existing.get("databases", [])) if existing else set()
    items = [(db, db, db in existing_dbs) for db in databases]
    selected = checklist("Select Databases", "Choose databases to backup:", items)
    if selected is None:
        return None
    if not selected:
        msg_box("Error", "Select at least one database.")
        return None
    return False, selected


def wizard_add_job() -> None:
    """Add backup job wizard."""
    name = input_box("Add Job", "Job name:")
    if not name or not name.strip():
        return

    conn = prompt_connection()
    if not conn:
        return

    ok, err = test_connection(conn)
    if not ok:
        msg_box("Connection Failed", f"Connection test failed:\n{err}\n\nJob not created.")
        return

    selection = prompt_database_selection(conn)
    if selection is None:
        return
    backup_all, databases = selection

    retention = prompt_retention()
    if retention is None:
        return

    job = {
        "id": str(uuid.uuid4()),
        "name": name.strip(),
        "database_type": conn["database_type"],
        "host": conn["host"],
        "port": conn["port"],
        "username": conn["username"],
        "password": conn["password"],
        "backup_all": backup_all,
        "databases": databases,
        "retention": retention,
    }

    config = load_config()
    config["jobs"].append(job)
    if save_config(config):
        msg_box("Success", f"Backup job '{job['name']}' created successfully.")
        log_message(f"Job created | name={job['name']} | id={job['id']}")


def select_job(title: str) -> Optional[Dict[str, Any]]:
    """Show job selection menu."""
    config = load_config()
    jobs = config.get("jobs", [])
    if not jobs:
        msg_box(title, "No backup jobs configured.")
        return None
    items = [(job["id"], f"{job['name']} ({DB_TYPES.get(job['database_type'], '?')})") for job in jobs]
    job_id = menu(title, items)
    if not job_id:
        return None
    return find_job(config, job_id)


def wizard_edit_job() -> None:
    """Edit an existing backup job."""
    config = load_config()
    job = select_job("Edit Backup Job")
    if not job:
        return

    while True:
        choice = menu(
            f"Edit: {job['name']}",
            [
                ("connection", "Edit connection information"),
                ("password", "Change password"),
                ("databases", "Change selected databases"),
                ("retention", "Change retention values"),
                ("done", "Save and return"),
            ],
            height=16,
        )
        if not choice or choice == "done":
            break

        if choice == "connection":
            conn = prompt_connection(job)
            if conn:
                ok, err = test_connection(conn)
                if not ok:
                    msg_box("Connection Failed", f"Connection test failed:\n{err}")
                    continue
                job["database_type"] = conn["database_type"]
                job["host"] = conn["host"]
                job["port"] = conn["port"]
                job["username"] = conn["username"]
                job["password"] = conn["password"]

        elif choice == "password":
            password = input_box("Password", "New password:", password=True)
            if password is not None:
                conn = build_connection_info(
                    job["database_type"],
                    job["host"],
                    job["port"],
                    job["username"],
                    password,
                )
                ok, err = test_connection(conn)
                if not ok:
                    msg_box("Connection Failed", f"Connection test failed:\n{err}")
                    continue
                job["password"] = password

        elif choice == "databases":
            conn = build_connection_info(
                job["database_type"],
                job["host"],
                job["port"],
                job["username"],
                job["password"],
            )
            selection = prompt_database_selection(conn, job)
            if selection:
                backup_all, databases = selection
                job["backup_all"] = backup_all
                job["databases"] = databases

        elif choice == "retention":
            retention = prompt_retention(job)
            if retention:
                job["retention"] = retention

    if save_config(config):
        msg_box("Success", f"Job '{job['name']}' updated.")
        log_message(f"Job updated | name={job['name']} | id={job['id']}")


def wizard_delete_job() -> None:
    """Delete a backup job with confirmation."""
    config = load_config()
    job = select_job("Delete Backup Job")
    if not job:
        return
    if yes_no(
        "Confirm Delete",
        f"Delete backup job '{job['name']}'?\n\nThis cannot be undone.",
        default="no",
    ):
        config["jobs"] = [item for item in config["jobs"] if item["id"] != job["id"]]
        if save_config(config):
            msg_box("Deleted", f"Job '{job['name']}' deleted.")
            log_message(f"Job deleted | name={job['name']} | id={job['id']}")


def menu_run_backup_now() -> None:
    """Run backup immediately for selected or all jobs."""
    config = load_config()
    if not config.get("jobs"):
        msg_box("Run Backup", "No backup jobs configured.")
        return

    choice = menu(
        "Run Backup Now",
        [
            ("all", "Run all jobs"),
            ("select", "Run selected job"),
        ],
    )
    if not choice:
        return

    if not acquire_lock():
        msg_box("Busy", "Backup already running.")
        return

    try:
        start = datetime.now()
        if choice == "all":
            any_failed = False
            total_size = 0
            summaries = []
            for job in config["jobs"]:
                result = run_job_backups(job)
                summaries.append(result)
                total_size += result["total_size"]
                if result["failed"] > 0:
                    any_failed = True
            send_telegram_run_report(summaries, start, datetime.now(), total_size)
            if any_failed:
                msg_box("Run Backup", "Backup completed with errors. Check logs.")
            else:
                msg_box("Run Backup", "All backups completed successfully.")
        else:
            job = select_job("Select Job")
            if not job:
                return
            result = run_job_backups(job)
            send_telegram_run_report([result], start, datetime.now(), result["total_size"])
            if result["failed"] > 0:
                msg_box(
                    "Run Backup",
                    f"Job '{job['name']}' completed with errors.\n\n"
                    + "\n".join(result["errors"][:5]),
                )
            else:
                msg_box("Run Backup", f"Job '{job['name']}' completed successfully.")
    finally:
        release_lock()


def menu_view_backup_usage() -> None:
    """Display backup storage usage."""
    daily = directory_size(DAILY_DIR)
    weekly = directory_size(WEEKLY_DIR)
    monthly = directory_size(MONTHLY_DIR)
    yearly = directory_size(YEARLY_DIR)
    total = directory_size(BACKUP_ROOT)
    message = (
        f"Daily Size:\n  {daily} bytes ({human_size(daily)})\n\n"
        f"Weekly Size:\n  {weekly} bytes ({human_size(weekly)})\n\n"
        f"Monthly Size:\n  {monthly} bytes ({human_size(monthly)})\n\n"
        f"Yearly Size:\n  {yearly} bytes ({human_size(yearly)})\n\n"
        f"Total Size:\n  {total} bytes ({human_size(total)})"
    )
    msg_box("Backup Usage", message, height=20, width=72)


def menu_show_logs() -> None:
    """Show recent log entries in scrollbox."""
    ensure_directories()
    try:
        content = LOG_FILE.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        content = f"Could not read log file: {exc}"
    if len(content) > 50000:
        content = content[-50000:]
    scroll_box("DBBackup Logs", content)


def menu_web_password() -> None:
    """Set the web dashboard username/password from the menu."""
    config = load_config()
    web = config.get("web", {})
    username = input_box("Web Dashboard", "Username:", web.get("username", "admin"))
    if username is None:
        return
    password = input_box("Web Dashboard", "Password:", password=True)
    if password is None:
        return
    if not password.strip():
        msg_box("Web Dashboard", "Password cannot be empty.")
        return
    confirm = input_box("Web Dashboard", "Confirm password:", password=True)
    if confirm is None:
        return
    if password != confirm:
        msg_box("Web Dashboard", "Passwords do not match.")
        return
    set_web_password(config, username.strip() or "admin", password)
    if save_config(config):
        msg_box(
            "Web Dashboard",
            f"Password saved for user '{username.strip() or 'admin'}'.\n\n"
            "Restart the dashboard service if running:\n"
            "  sudo systemctl restart dbbackup-web.service",
        )


# ---------------------------------------------------------------------------
# Systemd scheduler installation
# ---------------------------------------------------------------------------


def systemd_service_content() -> str:
    """Return dbbackup.service unit file contents."""
    script = SCRIPT_PATH
    if script.parent != BASE_DIR:
        script = BASE_DIR / "dbbackup.py"
    return f"""[Unit]
Description=DBBackup - Enterprise Database Backup Manager
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 {script} --run-scheduled
User=root
Group=root
Nice=10
IOSchedulingClass=best-effort
IOSchedulingPriority=7
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"""


def systemd_timer_content() -> str:
    """Return dbbackup.timer unit file contents."""
    # No Requires=dbbackup.service here: a timer that Requires its service
    # pulls the service in whenever the timer starts, i.e. a full backup run at
    # every boot. Unit= alone is what links the timer to the service.
    return """[Unit]
Description=DBBackup daily backup timer

[Timer]
OnCalendar=*-*-* 02:00:00
Persistent=true
Unit=dbbackup.service

[Install]
WantedBy=timers.target
"""


def install_scheduler() -> None:
    """Install or update systemd service and timer."""
    if os.geteuid() != 0:
        msg_box(
            "Root Required",
            "Installing the scheduler requires root privileges.\n\n"
            "Run: sudo python3 dbbackup.py",
        )
        return

    ensure_directories()
    installed_script = BASE_DIR / "dbbackup.py"
    if SCRIPT_PATH != installed_script and SCRIPT_PATH.exists():
        try:
            shutil.copy2(SCRIPT_PATH, installed_script)
            os.chmod(installed_script, 0o755)
            log_message(f"Installed script to {installed_script}")
        except OSError as exc:
            msg_box("Warning", f"Could not copy script to {installed_script}:\n{exc}")

    try:
        SYSTEMD_SERVICE.write_text(systemd_service_content(), encoding="utf-8")
        SYSTEMD_TIMER.write_text(systemd_timer_content(), encoding="utf-8")
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "enable", "dbbackup.timer"], check=True)
        subprocess.run(["systemctl", "restart", "dbbackup.timer"], check=True)
        log_message("Systemd scheduler installed/updated")
        msg_box(
            "Scheduler Installed",
            "Systemd timer installed successfully.\n\n"
            "Schedule: daily at 02:00\n"
            "Service: dbbackup.service\n"
            "Timer: dbbackup.timer\n\n"
            "Check status: systemctl status dbbackup.timer",
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        log_exception("Scheduler installation failed", exc)
        msg_box("Error", f"Failed to install scheduler:\n{exc}")


# ---------------------------------------------------------------------------
# Read-only web dashboard (dbbackup --serve)
# ---------------------------------------------------------------------------

WEB_CATEGORIES = ["daily", "weekly", "monthly", "yearly"]

WEB_CSS = """
:root{color-scheme:light dark}
*{box-sizing:border-box}
body{font:14px/1.5 system-ui,Segoe UI,Roboto,Helvetica,Arial,sans-serif;margin:0;
 background:#0f172a;color:#e2e8f0}
header{background:#1e293b;padding:16px 24px;border-bottom:1px solid #334155}
header h1{margin:0;font-size:18px}
header .sub{color:#94a3b8;font-size:12px;margin-top:4px}
main{padding:24px;max-width:1100px;margin:0 auto}
.cards{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:24px}
.card{background:#1e293b;border:1px solid #334155;border-radius:10px;padding:14px 18px;min-width:150px}
.card .n{font-size:22px;font-weight:600}
.card .l{color:#94a3b8;font-size:12px;text-transform:uppercase;letter-spacing:.04em}
h2{font-size:15px;margin:24px 0 8px;border-bottom:1px solid #334155;padding-bottom:6px}
table{width:100%;border-collapse:collapse;background:#1e293b;border-radius:8px;overflow:hidden}
th,td{padding:8px 12px;text-align:left;border-bottom:1px solid #273449;font-size:13px}
th{background:#273449;color:#cbd5e1;font-weight:600}
tr:last-child td{border-bottom:none}
.r{text-align:right;font-variant-numeric:tabular-nums}
.muted{color:#64748b}
a{color:#60a5fa;text-decoration:none}a:hover{text-decoration:underline}
pre{background:#0b1220;border:1px solid #334155;border-radius:8px;padding:12px;
 overflow:auto;max-height:320px;font-size:12px;color:#cbd5e1}
"""


def collect_backups(category: str) -> List[Dict[str, Any]]:
    """List backup artifacts in a category (excludes .sha256/.meta.json sidecars)."""
    target = category_directory(category)
    items: List[Dict[str, Any]] = []
    if not target.exists():
        return items
    for path in target.iterdir():
        name = path.name
        if name.endswith(".sha256") or name.endswith(".meta.json") or not path.is_file():
            continue
        try:
            size = path.stat().st_size
            mtime = path.stat().st_mtime
        except OSError:
            continue
        meta = {}
        meta_path = Path(f"{path}.meta.json")
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                meta = {}
        items.append(
            {
                "name": name,
                "size": size,
                "mtime": mtime,
                "database": meta.get("database", ""),
                "job_name": meta.get("job_name", ""),
                "db_type": meta.get("database_type", ""),
                "timestamp": meta.get("timestamp", ""),
            }
        )
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items


def safe_backup_path(rel: str) -> Optional[Path]:
    """Resolve a download request to a real file under BACKUP_ROOT, or None."""
    if not rel:
        return None
    root = BACKUP_ROOT.resolve()
    candidate = (root / rel).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def hash_web_password(password: str, salt: bytes, iterations: int) -> str:
    """Derive a hex PBKDF2-HMAC-SHA256 hash of a dashboard password."""
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations
    ).hex()


def set_web_password(config: Dict[str, Any], username: str, password: str) -> None:
    """Store a salted PBKDF2 hash of the dashboard password in the config."""
    salt = os.urandom(16)
    config["web"] = {
        "username": username or "admin",
        "salt": salt.hex(),
        "password_hash": hash_web_password(password, salt, WEB_PBKDF2_ITERATIONS),
        "iterations": WEB_PBKDF2_ITERATIONS,
    }


def web_password_is_set(config: Dict[str, Any]) -> bool:
    """True if a dashboard password has been configured."""
    web = config.get("web", {})
    return bool(web.get("password_hash") and web.get("salt"))


def verify_web_credentials(config: Dict[str, Any], username: str, password: str) -> bool:
    """Constant-time check of a username/password against the stored hash."""
    web = config.get("web", {})
    stored_hash = web.get("password_hash", "")
    salt_hex = web.get("salt", "")
    if not stored_hash or not salt_hex:
        return False
    user_ok = hmac.compare_digest(str(username), str(web.get("username", "admin")))
    try:
        salt = bytes.fromhex(salt_hex)
        iterations = int(web.get("iterations", WEB_PBKDF2_ITERATIONS))
    except (ValueError, TypeError):
        return False
    computed = hash_web_password(password, salt, iterations)
    # Evaluate both comparisons regardless to avoid early-exit timing leaks.
    pass_ok = hmac.compare_digest(computed, stored_hash)
    return user_ok and pass_ok


def render_dashboard_html() -> str:
    """Render the read-only dashboard page (never exposes passwords)."""
    config = load_config()
    jobs = config.get("jobs", [])
    esc = html.escape
    data = {c: collect_backups(c) for c in WEB_CATEGORIES}

    cards = [
        f"<div class='card'><div class='n'>{esc(human_size(directory_size(BACKUP_ROOT)))}</div>"
        f"<div class='l'>Total</div></div>"
    ]
    for c in WEB_CATEGORIES:
        size = directory_size(category_directory(c))
        cards.append(
            f"<div class='card'><div class='n'>{esc(human_size(size))}</div>"
            f"<div class='l'>{esc(c)} · {len(data[c])} files</div></div>"
        )

    sections = []
    for c in WEB_CATEGORIES:
        rows = []
        for it in data[c]:
            dl = "/download?file=" + urllib.parse.quote(f"{c}/{it['name']}")
            when = it["timestamp"] or datetime.fromtimestamp(it["mtime"]).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            rows.append(
                "<tr><td>" + esc(it["job_name"] or "-") + "</td><td>"
                + esc(it["database"] or "-") + "</td><td>"
                + esc(DB_TYPES.get(it["db_type"], it["db_type"]) or "-") + "</td><td>"
                + esc(when) + "</td><td class='r'>" + esc(human_size(it["size"]))
                + "</td><td><a href='" + dl + "'>download</a></td></tr>"
            )
        body = "".join(rows) or "<tr><td colspan='6' class='muted'>No backups</td></tr>"
        sections.append(
            f"<h2>{esc(c.capitalize())} <span class='muted'>({len(data[c])} files)</span></h2>"
            "<table><thead><tr><th>Job</th><th>Database</th><th>Type</th>"
            "<th>Time</th><th class='r'>Size</th><th>Download</th></tr></thead><tbody>"
            + body + "</tbody></table>"
        )

    job_rows = []
    for j in jobs:
        scope = "ALL" if j.get("backup_all") else ", ".join(j.get("databases", []))
        ret = j.get("retention", {})
        retstr = "/".join(
            str(retention_count(ret, cat)) for cat in WEB_CATEGORIES
        )
        job_rows.append(
            "<tr><td>" + esc(str(j.get("name", ""))) + "</td><td>"
            + esc(DB_TYPES.get(j.get("database_type", ""), str(j.get("database_type", ""))))
            + "</td><td>" + esc(f"{j.get('host', '')}:{j.get('port', '')}") + "</td><td>"
            + esc(scope or "-") + "</td><td>" + esc(retstr) + "</td></tr>"
        )
    jobs_table = (
        "<h2>Jobs</h2><table><thead><tr><th>Name</th><th>Type</th><th>Server</th>"
        "<th>Databases</th><th>Retention d/w/m/y</th></tr></thead><tbody>"
        + ("".join(job_rows) or "<tr><td colspan='5' class='muted'>No jobs</td></tr>")
        + "</tbody></table>"
    )

    try:
        log_tail = esc(
            "\n".join(
                LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-60:]
            )
        )
    except OSError:
        log_tail = "(no log)"

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>DBBackup</title><style>" + WEB_CSS + "</style></head><body>"
        "<header><h1>DBBackup Dashboard</h1>"
        f"<div class='sub'>{esc(socket.gethostname())} · {esc(now)} · read-only</div></header>"
        "<main><div class='cards'>" + "".join(cards) + "</div>"
        + jobs_table + "".join(sections)
        + "<h2>Recent log</h2><pre>" + log_tail + "</pre></main></body></html>"
    )


class DashboardHandler(http.server.BaseHTTPRequestHandler):
    """Read-only HTTP handler: dashboard page + validated backup downloads."""

    server_version = "DBBackup/1.0"

    def _send(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_file(self, path: Path) -> None:
        try:
            size = path.stat().st_size
            handle = open(path, "rb")
        except OSError:
            self._send(404, "text/plain; charset=utf-8", b"Not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header(
            "Content-Disposition", f'attachment; filename="{path.name}"'
        )
        self.end_headers()
        try:
            with handle:
                shutil.copyfileobj(handle, self.wfile, 256 * 1024)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _require_auth(self) -> bool:
        """HTTP Basic Auth against the stored dashboard password. 401 on failure."""
        config = load_config()
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                decoded = base64.b64decode(header[6:]).decode("utf-8", "replace")
                user, _, password = decoded.partition(":")
            except (ValueError, UnicodeError):
                user, password = "", ""
            if verify_web_credentials(config, user, password):
                return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="DBBackup"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        body = b"Unauthorized"
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
        return False

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/health":
            self._send(200, "text/plain; charset=utf-8", b"ok")
            return
        if not self._require_auth():
            return
        if parsed.path in ("/", "/index.html"):
            try:
                body = render_dashboard_html().encode("utf-8")
            except Exception as exc:  # never crash the server on a render error
                log_exception("Dashboard render failed", exc)
                self._send(500, "text/plain; charset=utf-8", b"Internal error")
                return
            self._send(200, "text/html; charset=utf-8", body)
        elif parsed.path == "/download":
            rel = urllib.parse.parse_qs(parsed.query).get("file", [""])[0]
            path = safe_backup_path(rel)
            if path is None:
                self._send(404, "text/plain; charset=utf-8", b"Not found")
            else:
                self._send_file(path)
        else:
            self._send(404, "text/plain; charset=utf-8", b"Not found")

    def log_message(self, fmt: str, *args: Any) -> None:
        # Quiet by default; nginx keeps the access log. Errors still surface.
        return


def run_web_server(
    bind: str = "0.0.0.0",
    port: int = 8080,
    tls_cert: Optional[str] = None,
    tls_key: Optional[str] = None,
) -> int:
    """Run the read-only dashboard (Basic Auth required). Returns an exit code."""
    ensure_directories()
    if not web_password_is_set(load_config()):
        console_msg(
            "Refusing to start: no dashboard password set.\n"
            "Set one first: sudo python3 /opt/dbbackup/dbbackup.py --set-web-password"
        )
        return 1
    try:
        httpd = http.server.ThreadingHTTPServer((bind, port), DashboardHandler)
    except OSError as exc:
        console_msg(f"Could not bind {bind}:{port}: {exc}")
        return 1
    scheme = "http"
    if tls_cert and tls_key:
        try:
            import ssl

            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(tls_cert, tls_key)
            httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
            scheme = "https"
        except (OSError, ValueError) as exc:
            console_msg(f"TLS setup failed: {exc}")
            httpd.server_close()
            return 1
    log_message(f"Web dashboard started on {scheme}://{bind}:{port}")
    console_msg(f"DBBackup dashboard: {scheme}://{bind}:{port}  (Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        console_msg("Dashboard stopped.")
    finally:
        httpd.server_close()
    return 0


# ---------------------------------------------------------------------------
# Main menu and entry point
# ---------------------------------------------------------------------------


def main_menu(skip_deps_check: bool = False) -> None:
    """Display interactive main menu loop (whiptail or text fallback)."""
    console_msg("DBBackup - Enterprise Database Backup Manager")

    if not ensure_interactive_terminal():
        sys.exit(1)

    if os.environ.get("DBBACKUP_TEXT_UI") == "1":
        enable_text_ui("DBBACKUP_TEXT_UI=1")
    elif not whiptail_available():
        console_msg("whiptail not installed — installing...")
        if os.geteuid() != 0:
            console_msg("Run with sudo, or use: --text-ui")
            sys.exit(1)
        install_dependencies(force_prompt=True, show_progress=True)
        if not whiptail_available():
            enable_text_ui("whiptail not available after install")

    if not skip_deps_check and not all_required_commands_available():
        install_dependencies(force_prompt=True, show_progress=True)
    elif not skip_deps_check and not deps_recently_verified():
        install_dependencies(force_prompt=True, show_progress=False)

    console_msg("Opening menu...")

    if not whiptail_available() and not TEXT_UI_MODE:
        console_msg("ERROR: No UI available.")
        sys.exit(1)

    while True:
        choice = menu(
            "Main Menu",
            [
                ("1", "Add Backup Job"),
                ("2", "Edit Backup Job"),
                ("3", "Delete Backup Job"),
                ("4", "Run Backup Now"),
                ("5", "View Backup Usage"),
                ("6", "Telegram Settings"),
                ("7", "Install Scheduler"),
                ("8", "Show Logs"),
                ("9", "Web Dashboard Password"),
                ("10", "Exit"),
            ],
        )
        if not choice or choice == "10":
            break
        try:
            if choice == "1":
                wizard_add_job()
            elif choice == "2":
                wizard_edit_job()
            elif choice == "3":
                wizard_delete_job()
            elif choice == "4":
                menu_run_backup_now()
            elif choice == "5":
                menu_view_backup_usage()
            elif choice == "6":
                menu_telegram_settings()
            elif choice == "7":
                install_scheduler()
            elif choice == "8":
                menu_show_logs()
            elif choice == "9":
                menu_web_password()
        except Exception as exc:
            log_exception("Unhandled menu error", exc)
            msg_box("Error", f"An unexpected error occurred:\n{exc}")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="DBBackup - Enterprise Database Backup Manager",
    )
    parser.add_argument(
        "--run-scheduled",
        action="store_true",
        help="Run all backup jobs (used by systemd timer)",
    )
    parser.add_argument(
        "--run-job",
        metavar="JOB_ID",
        help="Run a specific backup job by ID",
    )
    parser.add_argument(
        "--install-deps",
        action="store_true",
        help="Install missing dependencies and exit",
    )
    parser.add_argument(
        "--skip-deps-check",
        action="store_true",
        help="Skip dependency check on startup (interactive mode)",
    )
    parser.add_argument(
        "--test-ui",
        action="store_true",
        help="Test whiptail UI and exit (diagnostics)",
    )
    parser.add_argument(
        "--text-ui",
        action="store_true",
        help="Use plain text menus instead of whiptail",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Run the read-only web dashboard (Basic Auth required)",
    )
    parser.add_argument(
        "--set-web-password",
        action="store_true",
        help="Set the web dashboard username/password and exit",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Web dashboard port (default 8080)",
    )
    parser.add_argument(
        "--bind",
        default="0.0.0.0",
        help="Web dashboard bind address (default 0.0.0.0)",
    )
    parser.add_argument(
        "--tls-cert",
        help="Path to a TLS certificate (PEM) to serve the dashboard over HTTPS",
    )
    parser.add_argument(
        "--tls-key",
        help="Path to the TLS private key (PEM) for --tls-cert",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    """Application entry point."""
    ensure_directories()
    args = parse_args(argv)

    if args.install_deps:
        console_msg("DBBackup dependency installer")
        ok = install_dependencies(force_prompt=False, show_progress=True)
        if ok:
            console_msg("Done. All dependencies are installed.")
        else:
            console_msg("Failed. See log: /opt/dbbackup/logs/dbbackup.log")
        return 0 if ok else 1

    if args.test_ui:
        console_msg("DBBackup UI diagnostic")
        if not ensure_interactive_terminal():
            return 1
        if not whiptail_available():
            console_msg("FAIL: whiptail not installed")
            return 1
        console_msg("Step 1/2: msgbox — you should see a dialog now...")
        if not test_whiptail_ui():
            console_msg("FAIL: msgbox test failed or timed out")
            console_msg("Try: sudo -E python3 /opt/dbbackup/dbbackup.py --text-ui")
            return 1
        console_msg("Step 2/2: menu test...")
        code, choice = run_whiptail(
            [
                "--title",
                "Menu Test",
                "--menu",
                "Select option B:",
                "12",
                "50",
                "2",
                "a",
                "Option A",
                "b",
                "Option B",
            ],
            timeout=120,
        )
        console_msg(f"Menu returned code={code} choice={choice!r}")
        if code == 0 and choice == "b":
            console_msg("PASS: whiptail UI fully working")
            return 0
        console_msg("FAIL: menu test did not return expected value")
        return 1

    if args.text_ui:
        enable_text_ui("--text-ui flag")

    if args.set_web_password:
        config = load_config()
        try:
            username = (input("Web dashboard username [admin]: ").strip() or "admin")
        except EOFError:
            username = "admin"
        password = getpass.getpass("Web dashboard password: ")
        confirm = getpass.getpass("Confirm password: ")
        if not password:
            console_msg("Password cannot be empty.")
            return 1
        if password != confirm:
            console_msg("Passwords do not match.")
            return 1
        set_web_password(config, username, password)
        if not save_config(config):
            return 1
        console_msg(f"Web dashboard password set for user '{username}'.")
        return 0

    if args.serve:
        return run_web_server(args.bind, args.port, args.tls_cert, args.tls_key)

    if args.run_scheduled:
        install_dependencies(force_prompt=False, show_progress=False, only_for_jobs=True)
        return run_all_backups()

    if args.run_job:
        install_dependencies(force_prompt=False, show_progress=False, only_for_jobs=True)
        if not acquire_lock():
            print("Backup already running.", file=sys.stderr)
            return 1
        try:
            config = load_config()
            job = find_job(config, args.run_job)
            if not job:
                log_message(f"Job not found: {args.run_job}", "ERROR")
                return 1
            start = datetime.now()
            result = run_job_backups(job)
            send_telegram_run_report([result], start, datetime.now(), result["total_size"])
            if result["failed"] > 0:
                return 1
            return 0
        finally:
            release_lock()

    try:
        main_menu(skip_deps_check=args.skip_deps_check)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
