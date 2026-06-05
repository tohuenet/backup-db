#!/usr/bin/env python3
"""
DBBackup - Enterprise Database Backup Manager

Single-file terminal application for managing PostgreSQL, MySQL/MariaDB,
and MongoDB backups on Ubuntu 22.04 LTS. Uses whiptail for UI and systemd
timers for scheduling.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta
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
}

APT_PACKAGES = {
    "python3": "python3",
    "whiptail": "whiptail",
    "pg_dump": "postgresql-client",
    "mysqldump": "mysql-client",
    "gzip": "gzip",
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

DEFAULT_CONFIG: Dict[str, Any] = {
    "telegram": {
        "enabled": False,
        "bot_token": "",
        "chat_id": "",
    },
    "jobs": [],
}

# Fallback when whiptail cannot access the TTY (common under sudo/SSH).
TEXT_UI_MODE = False

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def ensure_directories() -> None:
    """Create required directory structure under /opt/dbbackup."""
    for directory in (BASE_DIR, DAILY_DIR, WEEKLY_DIR, MONTHLY_DIR, LOG_DIR):
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
    run_whiptail(
        [
            "--title",
            title,
            "--scrollbox",
            content,
            str(height),
            "78",
        ]
    )


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


def install_dependencies(force_prompt: bool = True, show_progress: bool = True) -> bool:
    """
    Detect and install missing required packages via apt.
    Requires root privileges.
    """
    missing_packages: List[str] = []
    for command, package in APT_PACKAGES.items():
        if command_exists(command):
            continue
        if package_installed(package):
            continue
        if package not in missing_packages:
            missing_packages.append(package)

    needs_mongodb = not mongodb_tools_available()

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
    return {"postgresql": 5432, "mysql": 3306, "mongodb": 27017}.get(db_type, 0)


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

        return False, f"Unsupported database type: {db_type}"
    except subprocess.TimeoutExpired:
        return False, "Connection timed out"
    except OSError as exc:
        return False, str(exc)


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
            return True, databases, ""

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
            return True, databases, ""

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
            return True, databases, ""

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
    """Verify backup integrity using gzip -t or tar -tzf."""
    try:
        if db_type in ("postgresql", "mysql"):
            result = subprocess.run(
                ["gzip", "-t", str(backup_path)],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                return False, (result.stderr or "gzip verification failed").strip()
            return True, ""

        if db_type == "mongodb":
            result = subprocess.run(
                ["tar", "-tzf", str(backup_path)],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                return False, (result.stderr or "tar verification failed").strip()
            return True, ""

        return False, f"Unknown database type: {db_type}"
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
    """Calculate total size of all files under a directory."""
    total = 0
    if not path.exists():
        return 0
    for root, _dirs, files in os.walk(path):
        for filename in files:
            file_path = Path(root) / filename
            try:
                total += file_path.stat().st_size
            except OSError:
                continue
    return total


def backup_categories_for_today(when: Optional[datetime] = None) -> List[str]:
    """
    Determine backup categories to create.
    Always daily; weekly on Sunday; monthly on day 1.
    """
    moment = when or datetime.now()
    categories = ["daily"]
    if moment.weekday() == 6:  # Sunday
        categories.append("weekly")
    if moment.day == 1:
        categories.append("monthly")
    return categories


def category_directory(category: str) -> Path:
    """Map category name to filesystem path."""
    mapping = {
        "daily": DAILY_DIR,
        "weekly": WEEKLY_DIR,
        "monthly": MONTHLY_DIR,
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
        conn = build_connection_info(
            job["database_type"],
            job["host"],
            job["port"],
            job["username"],
            job["password"],
        )
        ok, databases, err = discover_databases(conn)
        if not ok:
            raise RuntimeError(f"Failed to discover databases: {err}")
        return databases
    return list(job.get("databases", []))


def run_pg_dump(
    conn: Dict[str, Any],
    database: str,
    output_gz: Path,
) -> None:
    """Dump a PostgreSQL database to a gzip file."""
    env = os.environ.copy()
    env["PGPASSWORD"] = conn["password"]
    dump_cmd = [
        "pg_dump",
        "-h",
        conn["host"],
        "-p",
        str(conn["port"]),
        "-U",
        conn["username"],
        "-d",
        database,
        "--no-owner",
        "--no-privileges",
    ]
    with open(output_gz, "wb") as outfile:
        gzip_proc = subprocess.Popen(["gzip", "-c"], stdin=subprocess.PIPE, stdout=outfile)
        assert gzip_proc.stdin is not None
        dump_proc = subprocess.Popen(
            dump_cmd,
            stdout=gzip_proc.stdin,
            stderr=subprocess.PIPE,
            env=env,
        )
        gzip_proc.stdin.close()
        _dump_stderr = dump_proc.communicate()[1]
        gzip_rc = gzip_proc.wait()
        if dump_proc.returncode != 0:
            err = (_dump_stderr or b"pg_dump failed").decode(errors="replace")
            raise RuntimeError(err.strip())
        if gzip_rc != 0:
            raise RuntimeError("gzip compression failed")


def run_pg_globals(
    conn: Dict[str, Any],
    output_gz: Path,
) -> None:
    """Dump PostgreSQL global roles to a gzip file."""
    env = os.environ.copy()
    env["PGPASSWORD"] = conn["password"]
    cmd = [
        "pg_dumpall",
        "--globals-only",
        "-h",
        conn["host"],
        "-p",
        str(conn["port"]),
        "-U",
        conn["username"],
    ]
    with open(output_gz, "wb") as outfile:
        gzip_proc = subprocess.Popen(["gzip", "-c"], stdin=subprocess.PIPE, stdout=outfile)
        assert gzip_proc.stdin is not None
        dump_proc = subprocess.Popen(
            cmd,
            stdout=gzip_proc.stdin,
            stderr=subprocess.PIPE,
            env=env,
        )
        gzip_proc.stdin.close()
        _dump_stderr = dump_proc.communicate()[1]
        gzip_rc = gzip_proc.wait()
        if dump_proc.returncode != 0:
            err = (_dump_stderr or b"pg_dumpall failed").decode(errors="replace")
            raise RuntimeError(err.strip())
        if gzip_rc != 0:
            raise RuntimeError("gzip compression failed")


def run_mysqldump(
    conn: Dict[str, Any],
    database: str,
    output_gz: Path,
) -> None:
    """Dump a MySQL/MariaDB database to a gzip file."""
    env = os.environ.copy()
    env["MYSQL_PWD"] = conn["password"]
    dump_cmd = [
        "mysqldump",
        "-h",
        conn["host"],
        "-P",
        str(conn["port"]),
        "-u",
        conn["username"],
        "--single-transaction",
        "--routines",
        "--triggers",
        "--events",
        "--databases",
        database,
    ]
    with open(output_gz, "wb") as outfile:
        gzip_proc = subprocess.Popen(["gzip", "-c"], stdin=subprocess.PIPE, stdout=outfile)
        assert gzip_proc.stdin is not None
        dump_proc = subprocess.Popen(
            dump_cmd,
            stdout=gzip_proc.stdin,
            stderr=subprocess.PIPE,
            env=env,
        )
        gzip_proc.stdin.close()
        _dump_stderr = dump_proc.communicate()[1]
        gzip_rc = gzip_proc.wait()
        if dump_proc.returncode != 0:
            err = (_dump_stderr or b"mysqldump failed").decode(errors="replace")
            raise RuntimeError(err.strip())
        if gzip_rc != 0:
            raise RuntimeError("gzip compression failed")


def run_mongodump(
    conn: Dict[str, Any],
    database: str,
    output_tgz: Path,
) -> None:
    """Dump a MongoDB database and archive as tar.gz."""
    with tempfile.TemporaryDirectory(prefix="dbbackup_mongo_") as temp_dir:
        dump_dir = Path(temp_dir) / "dump"
        cmd = [
            "mongodump",
            "--host",
            conn["host"],
            "--port",
            str(conn["port"]),
            "-u",
            conn["username"],
            "-p",
            conn["password"],
            "--authenticationDatabase",
            "admin",
            "--db",
            database,
            "--out",
            str(dump_dir),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "mongodump failed").strip()
            raise RuntimeError(err)

        archive_cmd = ["tar", "-czf", str(output_tgz), "-C", str(dump_dir), "."]
        result = subprocess.run(archive_cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            err = (result.stderr or "tar archive failed").strip()
            raise RuntimeError(err)


def backup_extension(db_type: str) -> str:
    """Return backup file extension for database type."""
    if db_type == "mongodb":
        return "tar.gz"
    return "sql.gz"


def backup_one_database(
    job: Dict[str, Any],
    database: str,
    category: str,
    when: Optional[datetime] = None,
) -> Tuple[bool, str, int, float]:
    """
    Backup a single database for one category.
    Returns (success, error_message, size_bytes, duration_seconds).
    """
    moment = when or datetime.now()
    conn = build_connection_info(
        job["database_type"],
        job["host"],
        job["port"],
        job["username"],
        job["password"],
    )
    db_type = job["database_type"]
    ext = backup_extension(db_type)
    ts = timestamp_str(moment)
    safe_db = sanitize_name(database)
    prefix = job_backup_prefix(job)
    target_dir = category_directory(category)
    target_dir.mkdir(parents=True, exist_ok=True)
    backup_path = target_dir / f"{safe_db}_{ts}.{ext}"

    start = time.time()
    job_name = job.get("name", "unnamed")
    log_message(
        f"Starting backup | job={job_name} | db={database} | "
        f"category={category} | host={job.get('host')}"
    )

    try:
        if db_type == "postgresql":
            run_pg_dump(conn, database, backup_path)
        elif db_type == "mysql":
            run_mysqldump(conn, database, backup_path)
        elif db_type == "mongodb":
            run_mongodump(conn, database, backup_path)
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
            return False, verr, 0, duration

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
        return True, "", size_bytes, duration
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
        return False, err, 0, duration


def backup_postgresql_globals(
    job: Dict[str, Any],
    category: str,
    when: Optional[datetime] = None,
) -> Tuple[bool, str, int, float]:
    """Backup PostgreSQL global roles separately."""
    moment = when or datetime.now()
    conn = build_connection_info(
        job["database_type"],
        job["host"],
        job["port"],
        job["username"],
        job["password"],
    )
    ts = timestamp_str(moment)
    prefix = job_backup_prefix(job)
    target_dir = category_directory(category)
    target_dir.mkdir(parents=True, exist_ok=True)
    backup_path = target_dir / f"globals_{prefix}_{ts}.sql.gz"
    start = time.time()
    job_name = job.get("name", "unnamed")
    database = "globals"

    try:
        run_pg_globals(conn, backup_path)
        ok, verr = verify_backup(backup_path, "postgresql")
        if not ok:
            delete_backup_artifacts(backup_path)
            duration = time.time() - start
            send_telegram_failure(job, database, verr)
            return False, verr, 0, duration
        digest = write_sha256_sidecar(backup_path)
        write_metadata(backup_path, database, "postgresql", digest, job, moment)
        size_bytes = backup_path.stat().st_size
        duration = time.time() - start
        log_message(
            f"Globals backup completed | job={job_name} | duration={duration:.2f}s | "
            f"size={size_bytes}"
        )
        send_telegram_success(job, database, duration, size_bytes)
        return True, "", size_bytes, duration
    except Exception as exc:
        duration = time.time() - start
        if backup_path.exists():
            delete_backup_artifacts(backup_path)
        err = str(exc)
        log_exception(f"Globals backup failed | job={job_name}", exc)
        send_telegram_failure(job, database, err)
        return False, err, 0, duration


def backup_path_from_meta(meta_path: Path) -> Path:
    """Resolve backup file path from its .meta.json sidecar path."""
    name = meta_path.name
    if name.endswith(".meta.json"):
        backup_name = name[: -len(".meta.json")]
        return meta_path.with_name(backup_name)
    return meta_path


def apply_retention(job: Dict[str, Any]) -> None:
    """Delete expired backups for a job based on retention settings."""
    retention = job.get("retention", {})
    daily_days = int(retention.get("daily_days", 14))
    weekly_weeks = int(retention.get("weekly_weeks", 8))
    monthly_months = int(retention.get("monthly_months", 12))
    job_id = job.get("id", "")
    now = datetime.now()

    rules = [
        ("daily", daily_days, "days"),
        ("weekly", weekly_weeks, "weeks"),
        ("monthly", monthly_months, "months"),
    ]

    for category, keep_count, unit in rules:
        target_dir = category_directory(category)
        if not target_dir.exists():
            continue
        if unit == "days":
            cutoff = now - timedelta(days=keep_count)
        elif unit == "weeks":
            cutoff = now - timedelta(weeks=keep_count)
        else:
            cutoff = now - timedelta(days=keep_count * 30)

        for meta_path in target_dir.glob("*.meta.json"):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if meta.get("job_id") != job_id:
                continue
            timestamp_raw = meta.get("timestamp", "")
            try:
                backup_time = datetime.fromisoformat(timestamp_raw)
            except ValueError:
                try:
                    backup_time = datetime.fromtimestamp(meta_path.stat().st_mtime)
                except OSError:
                    continue
            if backup_time >= cutoff:
                continue
            backup_path = backup_path_from_meta(meta_path)
            log_message(
                f"Retention delete | job={job.get('name')} | "
                f"file={backup_path.name} | category={category}"
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
        "success": 0,
        "failed": 0,
        "total_size": 0,
        "errors": [],
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
        log_exception(f"Job discovery failed | job={summary['job']}", exc)
        send_telegram_failure(job, "discovery", err)
        return summary

    if not databases and job.get("database_type") != "postgresql":
        err = "No databases selected or discovered"
        summary["errors"].append(err)
        summary["failed"] += 1
        send_telegram_failure(job, "all", err)
        return summary

    for category in cats:
        if job.get("database_type") == "postgresql":
            ok, err, size, _duration = backup_postgresql_globals(job, category, moment)
            if ok:
                summary["success"] += 1
                summary["total_size"] += size
            else:
                summary["failed"] += 1
                summary["errors"].append(err)

        for database in databases:
            ok, err, size, _duration = backup_one_database(
                job, database, category, moment
            )
            if ok:
                summary["success"] += 1
                summary["total_size"] += size
            else:
                summary["failed"] += 1
                summary["errors"].append(err)

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
        for job in config["jobs"]:
            result = run_job_backups(job, when)
            total_size += result["total_size"]
            if result["failed"] > 0:
                any_failed = True

        send_telegram_daily_summary(total_size)
        end = datetime.now()
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


def send_telegram_message(text: str, config: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
    """Send a message via Telegram Bot API."""
    cfg = config or load_config()
    telegram = cfg.get("telegram", {})
    if not telegram.get("enabled"):
        return False, "Telegram disabled"
    token = telegram.get("bot_token", "").strip()
    chat_id = telegram.get("chat_id", "").strip()
    if not token or not chat_id:
        return False, "Telegram not configured"

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
        }
    ).encode("utf-8")
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
    """Send backup success notification."""
    config = load_config()
    if not config.get("telegram", {}).get("enabled"):
        return
    server = f"{job.get('host')}:{job.get('port')}"
    text = (
        "<b>Backup Success</b>\n"
        f"Server: {server}\n"
        f"Database: {database}\n"
        f"Duration: {duration:.2f}s\n"
        f"Backup Size: {human_size(size_bytes)} ({size_bytes} bytes)"
    )
    ok, err = send_telegram_message(text, config)
    if not ok:
        log_message(f"Telegram success notification failed: {err}", "WARNING")


def send_telegram_failure(
    job: Dict[str, Any],
    database: str,
    error: str,
) -> None:
    """Send backup failure notification."""
    config = load_config()
    if not config.get("telegram", {}).get("enabled"):
        return
    server = f"{job.get('host')}:{job.get('port')}"
    text = (
        "<b>Backup Failed</b>\n"
        f"Server: {server}\n"
        f"Database: {database}\n"
        f"Error: {error}"
    )
    ok, err = send_telegram_message(text, config)
    if not ok:
        log_message(f"Telegram failure notification failed: {err}", "WARNING")


def send_telegram_daily_summary(run_total_size: int = 0) -> None:
    """Send daily summary with backup usage sizes."""
    config = load_config()
    if not config.get("telegram", {}).get("enabled"):
        return
    daily = directory_size(DAILY_DIR)
    weekly = directory_size(WEEKLY_DIR)
    monthly = directory_size(MONTHLY_DIR)
    total = directory_size(BACKUP_ROOT)
    text = (
        "<b>Daily Backup Summary</b>\n"
        f"Run Total Size: {human_size(run_total_size)} ({run_total_size} bytes)\n"
        f"Daily Size: {human_size(daily)} ({daily} bytes)\n"
        f"Weekly Size: {human_size(weekly)} ({weekly} bytes)\n"
        f"Monthly Size: {human_size(monthly)} ({monthly} bytes)\n"
        f"Total Size: {human_size(total)} ({total} bytes)"
    )
    ok, err = send_telegram_message(text, config)
    if not ok:
        log_message(f"Telegram summary failed: {err}", "WARNING")


def menu_telegram_settings() -> None:
    """Configure Telegram notifications through whiptail wizard."""
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
    """Prompt for retention settings."""
    current = existing.get("retention", {}) if existing else {}
    daily_default = str(current.get("daily_days", 14))
    weekly_default = str(current.get("weekly_weeks", 8))
    monthly_default = str(current.get("monthly_months", 12))

    daily_str = input_box("Retention", "Daily retention (days):", daily_default)
    if daily_str is None:
        return None
    weekly_str = input_box("Retention", "Weekly retention (weeks):", weekly_default)
    if weekly_str is None:
        return None
    monthly_str = input_box("Retention", "Monthly retention (months):", monthly_default)
    if monthly_str is None:
        return None

    try:
        return {
            "daily_days": int(daily_str.strip()),
            "weekly_weeks": int(weekly_str.strip()),
            "monthly_months": int(monthly_str.strip()),
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
        if choice == "all":
            any_failed = False
            total_size = 0
            for job in config["jobs"]:
                result = run_job_backups(job)
                total_size += result["total_size"]
                if result["failed"] > 0:
                    any_failed = True
            send_telegram_daily_summary(total_size)
            if any_failed:
                msg_box("Run Backup", "Backup completed with errors. Check logs.")
            else:
                msg_box("Run Backup", "All backups completed successfully.")
        else:
            job = select_job("Select Job")
            if not job:
                return
            result = run_job_backups(job)
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
    total = directory_size(BACKUP_ROOT)
    message = (
        f"Daily Size:\n  {daily} bytes ({human_size(daily)})\n\n"
        f"Weekly Size:\n  {weekly} bytes ({human_size(weekly)})\n\n"
        f"Monthly Size:\n  {monthly} bytes ({human_size(monthly)})\n\n"
        f"Total Size:\n  {total} bytes ({human_size(total)})"
    )
    msg_box("Backup Usage", message, height=18, width=72)


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
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"""


def systemd_timer_content() -> str:
    """Return dbbackup.timer unit file contents."""
    return """[Unit]
Description=DBBackup daily backup timer
Requires=dbbackup.service

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
                ("9", "Exit"),
            ],
        )
        if not choice or choice == "9":
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

    if args.run_scheduled:
        install_dependencies(force_prompt=False, show_progress=False)
        return run_all_backups()

    if args.run_job:
        install_dependencies(force_prompt=False, show_progress=False)
        if not acquire_lock():
            print("Backup already running.", file=sys.stderr)
            return 1
        try:
            config = load_config()
            job = find_job(config, args.run_job)
            if not job:
                log_message(f"Job not found: {args.run_job}", "ERROR")
                return 1
            result = run_job_backups(job)
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
