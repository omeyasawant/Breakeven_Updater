#!/usr/bin/env python
# coding: utf-8

# In[ ]:


'''
updater.py
    │
    ├─ read ../client_config.json
    │
    ├─ detect OS
    │
    ├─ fetch
    │   https://updates.breakeventx.com/sudo_manifest.json
    │
    ├─ check latest version for OS
    │
    ├─ if newer version:
    │        download
    │        https://updates.breakeventx.com/vX.X.X.X/<os>/
    │
    ├─ replace install directory files
    │
    └─ copy client_service → serviceInstallPath
'''


# In[ ]:


import os
import json
import time
import hashlib
import logging
import platform
import re
import shutil
import subprocess
import sys
from datetime import datetime
from datetime import timedelta
from html import unescape
from urllib.parse import urljoin

import psutil
import requests
from packaging import version


# In[ ]:


#CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "client_config.json")

UPDATES_INDEX_URL = "https://updates.breakeventx.com"
MANIFEST_NAME = "sudo_manifest.json"

# Final fallback if index parsing fails
DOWNLOAD_BASE_DEFAULT = "https://data.breakeventx.com:64444/content-cache/updates"
MANIFEST_URL_DEFAULT = f"{DOWNLOAD_BASE_DEFAULT}/{MANIFEST_NAME}"

CHECK_INTERVAL = 3600

HELPER_STATE_ARG = "--apply-update-state"
RUNTIME_BASE_ENV = "BREAKEVEN_UPDATER_RUNTIME_BASE"
HELPER_STATE_ENV = "BREAKEVEN_UPDATER_STATE_PATH"
HELPER_BOOTSTRAP_LOG_ENV = "BREAKEVEN_UPDATER_BOOTSTRAP_LOG"
SERVICE_MANIFEST_FILES = (
    "service_manifest.json",
    "tray_service_manifest.json",
    "updater_service_manifest.json",
)
WINDOWS_DETACHED_FLAGS = (
    getattr(subprocess, "DETACHED_PROCESS", 0)
    | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
)

LAST_UP_TO_DATE_LOG_DATE = None


def write_bootstrap_trace(message):
    bootstrap_log_path = os.environ.get(HELPER_BOOTSTRAP_LOG_ENV)
    if not bootstrap_log_path:
        return

    try:
        os.makedirs(os.path.dirname(bootstrap_log_path), exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(bootstrap_log_path, "a", encoding="utf-8") as f:
            f.write(f"{timestamp} | {message}\n")
    except Exception:
        pass

'''
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
LOG_PATH = os.path.join(LOG_DIR, "updater.log")
'''


# In[ ]:


def get_runtime_base_dir():
    """
    Returns the real folder where the updater is located.
    Works for both:
    - updater.py run directly
    - PyInstaller-built .exe
    """
    override_base_dir = os.environ.get(RUNTIME_BASE_ENV)
    if override_base_dir:
        write_bootstrap_trace(f"Using runtime base override: {override_base_dir}")
        return os.path.abspath(override_base_dir)

    if getattr(sys, "frozen", False):
        write_bootstrap_trace(f"Using frozen executable runtime base: {sys.executable}")
        return os.path.dirname(os.path.abspath(sys.executable))
    write_bootstrap_trace(f"Using script runtime base: {__file__}")
    return os.path.dirname(os.path.abspath(__file__))


RUNTIME_BASE_DIR = get_runtime_base_dir()

def resolve_config_path():
    """
    Priority:
    1. ../client_config.json relative to updater location
    2. ./client_config.json beside updater
    3. installPath/client_config.json if discoverable later
    """
    candidates = [
        os.path.normpath(os.path.join(RUNTIME_BASE_DIR, "..", "client_config.json")),
        os.path.normpath(os.path.join(RUNTIME_BASE_DIR, "client_config.json")),
    ]

    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate

    # default to the original intended relative path even if not found yet
    return candidates[0]


CONFIG_PATH = resolve_config_path()


# In[ ]:


BASE_DIR = RUNTIME_BASE_DIR
LOG_DIR = os.path.join(BASE_DIR, "logs")
LOG_PATH = os.path.join(LOG_DIR, "updater.log")


# In[ ]:


def setup_logger():
    os.makedirs(LOG_DIR, exist_ok=True)
    write_bootstrap_trace(f"Initializing logger at: {LOG_PATH}")

    logger = logging.getLogger("breakeven_updater")
    logger.setLevel(logging.INFO)

    if not logger.handlers:
        formatter = logging.Formatter(
            fmt="%(asctime)s | %(levelname)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )

        file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
        file_handler.setFormatter(formatter)

        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)

        logger.addHandler(file_handler)
        logger.addHandler(stream_handler)

    return logger


LOGGER = setup_logger()


# In[ ]:


def log_info(message):
    LOGGER.info(message)


def log_error(message):
    LOGGER.error(message)


# In[ ]:


def log_up_to_date_once_per_day(message):
    global LAST_UP_TO_DATE_LOG_DATE

    today = datetime.now().strftime("%Y-%m-%d")
    if LAST_UP_TO_DATE_LOG_DATE != today:
        log_info(message)
        LAST_UP_TO_DATE_LOG_DATE = today


# In[ ]:


def get_os_type():
    os_name = platform.system().lower()

    if os_name == "windows":
        return "windows"
    elif os_name == "linux":
        return "linux"
    elif os_name == "darwin":
        return "macos"
    else:
        raise Exception(f"Unsupported OS: {os_name}")


# In[ ]:


def get_config():
    write_bootstrap_trace(f"Loading config from: {CONFIG_PATH}")
    log_info(f"Using client config path: {CONFIG_PATH}")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


# In[ ]:


def update_config_version_at_path(config_path, new_version):
    if not config_path:
        return

    if not os.path.exists(config_path):
        log_info(f"Config file not found, skipping version update: {config_path}")
        return

    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    config["version"] = new_version

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)

    log_info(f"Updated version to {new_version} in {config_path}")


# In[ ]:


def resolve_manifest_url():
    """
    Priority:
    1. Clickable href for sudo_manifest.json from updates index
    2. Endpoint shown on updates index + /sudo_manifest.json
    3. Hardcoded MANIFEST_URL_DEFAULT
    """
    try:
        log_info(f"Resolving manifest URL from index: {UPDATES_INDEX_URL}")
        r = requests.get(UPDATES_INDEX_URL, timeout=15)
        r.raise_for_status()
        html = r.text

        # Priority 1: try to find clickable href for sudo_manifest.json
        href_match = re.search(
            rf'href=["\\\']([^"\\\']*{re.escape(MANIFEST_NAME)}[^"\\\']*)["\\\']',
            html,
            flags=re.IGNORECASE
        )
        if href_match:
            href = href_match.group(1).strip()
            manifest_url = urljoin(UPDATES_INDEX_URL + "/", href)
            log_info(f"Resolved manifest URL from clickable link: {manifest_url}")
            return manifest_url

        # Priority 2: parse displayed endpoint and build manifest URL from it
        endpoint_match = re.search(
            r'Endpoint:\s*(https?://[^<\s]+)',
            html,
            flags=re.IGNORECASE
        )
        if endpoint_match:
            endpoint = endpoint_match.group(1).strip().rstrip("/")
            manifest_url = f"{endpoint}/content-cache/updates/{MANIFEST_NAME}"
            log_info(f"Resolved manifest URL from displayed endpoint: {manifest_url}")
            return manifest_url

    except Exception as e:
        log_error(f"Failed resolving manifest URL from index: {e}")

    log_info(f"Falling back to default manifest URL: {MANIFEST_URL_DEFAULT}")
    return MANIFEST_URL_DEFAULT


# In[ ]:


def fetch_manifest():
    manifest_url = resolve_manifest_url()

    try:
        log_info(f"Fetching sudo_manifest.json from: {manifest_url}")
        r = requests.get(manifest_url, timeout=20)
        r.raise_for_status()
        manifest = r.json()

        # remember source URL for logging/debugging
        manifest["_resolved_manifest_url"] = manifest_url

        return manifest
    except Exception as e:
        log_error(f"[Updater] Failed to fetch manifest: {e}")
        return None


# In[ ]:


def resolve_clickable_file_url(version_path, relative_path):
    """
    Priority:
    1. Find clickable link for the file from the relevant index page on updates.breakeventx.com
    2. Fall back to direct object URL
    """
    relative_path = relative_path.replace("\\", "/").lstrip("/")
    version_path = version_path.strip("/")

    path_parts = relative_path.split("/")
    file_name = path_parts[-1]

    # Open the directory index page that should contain the file link
    if len(path_parts) > 1:
        directory_prefix = "/".join(path_parts[:-1]) + "/"
    else:
        directory_prefix = ""

    index_url = f"{UPDATES_INDEX_URL}/?prefix={version_path}/"
    if directory_prefix:
        index_url = f"{UPDATES_INDEX_URL}/?prefix={version_path}/{directory_prefix}"

    try:
        log_info(f"Resolving clickable file URL from index: {index_url}")
        r = requests.get(index_url, timeout=20)
        r.raise_for_status()
        html = r.text

        # Look for href containing the exact file name
        href_match = re.search(
            rf'href=["\\\']([^"\\\']*{re.escape(file_name)}[^"\\\']*)["\\\']',
            html,
            flags=re.IGNORECASE
        )
        if href_match:
            href = unescape(href_match.group(1).strip())
            file_url = urljoin(UPDATES_INDEX_URL + "/", href)
            log_info(f"Resolved clickable file URL: {file_url}")
            return file_url

    except Exception as e:
        log_error(f"Failed resolving clickable file URL for {relative_path}: {e}")

    fallback_url = build_download_url(DOWNLOAD_BASE_DEFAULT, version_path, relative_path)
    log_info(f"Falling back to direct file URL: {fallback_url}")
    return fallback_url


# In[ ]:


def resolve_file_download_url(manifest, version_path, file_info):
    """
    Priority:
    1. Explicit per-file URL from manifest
    2. Clickable link resolved from updates index
    3. Direct object URL from download_base/default
    """
    explicit_url = (file_info.get("url") or "").strip()
    if explicit_url:
        log_info(f"Using manifest-provided file URL for {file_info['relative_path']}: {explicit_url}")
        return explicit_url

    clickable_url = resolve_clickable_file_url(version_path, file_info["relative_path"])
    if clickable_url:
        return clickable_url

    download_base = manifest.get("download_base") or DOWNLOAD_BASE_DEFAULT
    return build_download_url(download_base, version_path, file_info["relative_path"])


# In[ ]:


def is_update_available(local_version, latest_version):
    return version.parse(latest_version) > version.parse(local_version)


# In[ ]:


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest().lower()


# In[ ]:


def build_download_url(download_base, version_path, relative_path):
    relative_path = relative_path.replace("\\", "/").lstrip("/")
    version_path = version_path.strip("/")
    return f"{download_base}/{version_path}/{relative_path}"


def verify_checksum(file_path, expected_sha256):
    actual_sha256 = sha256_file(file_path)
    expected_sha256 = expected_sha256.lower().strip()

    if actual_sha256 != expected_sha256:
        log_error(
            f"Checksum mismatch for {file_path}. "
            f"Expected {expected_sha256}, got {actual_sha256}"
        )
        raise RuntimeError(
            f"Checksum mismatch for {file_path}. "
            f"Expected {expected_sha256}, got {actual_sha256}"
        )

    log_info(f"Checksum OK for {file_path}")


def download_file_verified(url, dest_path, expected_sha256):
    log_info(f"Downloading: {url} -> {dest_path}")

    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    temp_path = dest_path + ".download"

    try:
        with requests.get(url, stream=True, timeout=60) as res:
            res.raise_for_status()
            with open(temp_path, "wb") as f:
                for chunk in res.iter_content(1024 * 1024):
                    if chunk:
                        f.write(chunk)

        verify_checksum(temp_path, expected_sha256)
        log_info(f"Checksum verified: {dest_path}")

        if os.path.exists(dest_path):
            os.remove(dest_path)

        os.replace(temp_path, dest_path)

    except Exception:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass
        raise


# In[ ]:


def parse_helper_state_path(argv=None):
    argv = argv or sys.argv

    env_state_path = os.environ.get(HELPER_STATE_ENV)
    if env_state_path:
        return env_state_path

    if HELPER_STATE_ARG not in argv:
        return None

    index = argv.index(HELPER_STATE_ARG)
    if index + 1 >= len(argv):
        raise RuntimeError(f"Missing state file path after {HELPER_STATE_ARG}")

    return argv[index + 1]


def normalize_fs_path(path_value):
    return os.path.normcase(os.path.abspath(path_value))


def is_path_within_root(path_value, root_path):
    try:
        return os.path.commonpath([normalize_fs_path(path_value), normalize_fs_path(root_path)]) == normalize_fs_path(root_path)
    except Exception:
        return False


def build_subprocess_kwargs(detached=False):
    kwargs = {}

    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        kwargs["startupinfo"] = startupinfo
        if detached:
            kwargs["creationflags"] = WINDOWS_DETACHED_FLAGS
    elif detached:
        kwargs["start_new_session"] = True

    return kwargs


def truncate_for_log(value, limit=600):
    if value is None:
        return ""

    value = str(value).strip()
    if len(value) <= limit:
        return value

    return value[:limit] + "..."


def format_command(command):
    try:
        return subprocess.list2cmdline(command)
    except Exception:
        return " ".join(str(part) for part in command)


def run_command_capture(command, timeout=30):
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        **build_subprocess_kwargs(),
    )


def append_helper_bootstrap_log(log_path, message):
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | {message}\n")
    except Exception:
        pass


def run_command_checked(command, timeout=30, context=None, log_path=None):
    result = run_command_capture(command, timeout=timeout)

    context = context or format_command(command)
    stdout = truncate_for_log(result.stdout)
    stderr = truncate_for_log(result.stderr)
    if stdout:
        log_info(f"{context} stdout: {stdout}")
        if log_path:
            append_helper_bootstrap_log(log_path, f"{context} stdout: {stdout}")
    if stderr:
        if result.returncode == 0:
            log_info(f"{context} stderr: {stderr}")
        else:
            log_error(f"{context} stderr: {stderr}")
        if log_path:
            append_helper_bootstrap_log(log_path, f"{context} stderr: {stderr}")

    if result.returncode != 0:
        raise RuntimeError(f"{context} failed with exit code {result.returncode}")

    return result


def spawn_detached(command, cwd=None, env=None):
    return subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        **build_subprocess_kwargs(detached=True),
    )


def windows_quote_arg(value):
    return subprocess.list2cmdline([str(value)])


def create_windows_helper_launcher(helper_dir, command, helper_env):
    launcher_path = os.path.join(helper_dir, "launch_helper.cmd")
    program_path = str(command[0])
    command_args = [str(part) for part in command[1:]]
    quoted_program = windows_quote_arg(program_path)
    quoted_args = " ".join(windows_quote_arg(arg) for arg in command_args)
    bootstrap_log = helper_env[HELPER_BOOTSTRAP_LOG_ENV]
    lines = [
        "@echo off",
        "setlocal",
        f'set "{RUNTIME_BASE_ENV}={helper_env[RUNTIME_BASE_ENV]}"',
        f'set "{HELPER_STATE_ENV}={helper_env[HELPER_STATE_ENV]}"',
        f'set "{HELPER_BOOTSTRAP_LOG_ENV}={helper_env[HELPER_BOOTSTRAP_LOG_ENV]}"',
        f'echo %date% %time% ^| Launcher entrypoint reached>>"{bootstrap_log}"',
        f'echo %date% %time% ^| Launcher working directory {helper_dir}>>"{bootstrap_log}"',
        f'if not exist {quoted_program} echo %date% %time% ^| Helper executable missing: {program_path}>>"{bootstrap_log}"',
        f'cd /d "{helper_dir}"',
        f'echo %date% %time% ^| Launching helper executable {program_path}>>"{bootstrap_log}"',
        f'start "" /b {quoted_program} {quoted_args}',
        f'echo %date% %time% ^| START command exit code %ERRORLEVEL%>>"{bootstrap_log}"',
        "exit /b %ERRORLEVEL%",
    ]
    with open(launcher_path, "w", encoding="utf-8") as f:
        f.write("\r\n".join(lines) + "\r\n")
    return launcher_path


def launch_helper_via_schtasks(command, helper_dir, helper_env):
    task_name = f"BreakEvenUpdaterHelper_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{os.getpid()}"
    bootstrap_log_path = helper_env[HELPER_BOOTSTRAP_LOG_ENV]
    launcher_path = create_windows_helper_launcher(helper_dir, command, helper_env)
    task_start = (datetime.now() + timedelta(minutes=1)).strftime("%H:%M")

    create_command = [
        "schtasks.exe",
        "/Create",
        "/F",
        "/TN",
        task_name,
        "/SC",
        "ONCE",
        "/ST",
        task_start,
        "/RL",
        "HIGHEST",
        "/RU",
        "SYSTEM",
        "/TR",
        launcher_path,
    ]
    append_helper_bootstrap_log(bootstrap_log_path, f"Creating scheduled helper task: {task_name} -> {launcher_path}")
    run_command_checked(
        create_command,
        timeout=30,
        context=f"schtasks create {task_name}",
        log_path=bootstrap_log_path,
    )

    run_command_checked(
        ["schtasks.exe", "/Run", "/TN", task_name],
        timeout=30,
        context=f"schtasks run {task_name}",
        log_path=bootstrap_log_path,
    )

    run_command_checked(
        ["schtasks.exe", "/Query", "/TN", task_name, "/V", "/FO", "LIST"],
        timeout=30,
        context=f"schtasks query {task_name}",
        log_path=bootstrap_log_path,
    )

    append_helper_bootstrap_log(bootstrap_log_path, f"Scheduled helper task created: {task_name}")

    return {"task_name": task_name, "pid": None, "launcher_path": launcher_path}


def launch_helper_process(command, helper_dir, helper_env):
    if os.name == "nt":
        return launch_helper_via_schtasks(command, helper_dir, helper_env)

    proc = spawn_detached(command, cwd=helper_dir, env=helper_env)
    return {"task_name": None, "pid": proc.pid}


def wait_for_helper_startup(bootstrap_log_path, timeout=20):
    deadline = time.time() + timeout
    success_markers = [
        "Launcher entrypoint reached",
        "Launching helper executable",
        "Process entrypoint reached",
        "Entered helper mode",
        "Helper state file loaded successfully",
    ]

    while time.time() < deadline:
        if os.path.exists(bootstrap_log_path):
            try:
                with open(bootstrap_log_path, "r", encoding="utf-8") as f:
                    contents = f.read()
            except Exception:
                contents = ""

            if any(marker in contents for marker in success_markers):
                return True, contents

        time.sleep(1)

    if os.path.exists(bootstrap_log_path):
        try:
            with open(bootstrap_log_path, "r", encoding="utf-8") as f:
                return False, f.read()
        except Exception:
            pass

    return False, ""


def resolve_manifest_target_path(manifest_path, target_path):
    if not target_path:
        return None

    normalized_target = target_path.replace("/", os.sep)
    if os.path.isabs(normalized_target):
        return os.path.normpath(normalized_target)

    manifest_root = os.path.dirname(os.path.dirname(manifest_path))
    return os.path.normpath(os.path.join(manifest_root, normalized_target))


def load_service_manifest_record(manifest_path):
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    commands = manifest.get("control", {}).get("commands", {}) or {}
    record = {
        "manifest_path": manifest_path,
        "manifest_name": os.path.basename(manifest_path),
        "name": manifest.get("name") or os.path.splitext(os.path.basename(manifest_path))[0],
        "identifier": manifest.get("identifier") or "",
        "service_type": manifest.get("serviceType") or "",
        "commands": commands,
        "binary_path": resolve_manifest_target_path(manifest_path, manifest.get("binary")),
        "runner_path": resolve_manifest_target_path(manifest_path, manifest.get("runner")),
        "log_file": resolve_manifest_target_path(manifest_path, manifest.get("logFile")),
    }
    hint_text = " ".join(
        part.lower()
        for part in [record["manifest_name"], record["name"], record["identifier"]]
        if part
    )
    record["is_updater"] = "updater" in hint_text
    return record


def discover_service_manifest_records(config):
    roots = [config.get("installPath"), config.get("serviceInstallPath")]
    records = []
    seen_keys = set()

    for root_path in roots:
        if not root_path:
            continue

        client_service_dir = os.path.join(root_path, "client_service")
        for manifest_name in SERVICE_MANIFEST_FILES:
            manifest_path = os.path.join(client_service_dir, manifest_name)
            if not os.path.exists(manifest_path):
                continue

            try:
                record = load_service_manifest_record(manifest_path)
            except Exception as e:
                log_error(f"Failed to load service manifest {manifest_path}: {e}")
                continue

            key = (
                record["service_type"],
                record["identifier"],
                record["binary_path"],
                record["runner_path"],
            )
            if key in seen_keys:
                continue

            seen_keys.add(key)
            records.append(record)

    if records:
        manifest_names = ", ".join(record["manifest_name"] for record in records)
        log_info(f"Discovered service manifests: {manifest_names}")
    else:
        log_info("No service manifests discovered for this install")

    return records


def get_service_status_command(record):
    identifier = record.get("identifier", "")
    service_type = record.get("service_type")

    if service_type == "windows-service":
        escaped = identifier.replace("'", "''")
        return [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-Command",
            (
                f"$svc = Get-Service -Name '{escaped}' -ErrorAction SilentlyContinue; "
                "if ($null -eq $svc) { exit 3 }; "
                "Write-Output $svc.Status"
            ),
        ]

    if service_type == "systemd-user-service" and identifier:
        return ["systemctl", "--user", "is-active", identifier]

    if service_type == "launch-agent" and identifier:
        return ["launchctl", "list", identifier]

    return record.get("commands", {}).get("status")


def is_service_running(record):
    status_command = get_service_status_command(record)
    if not status_command:
        return False

    try:
        result = run_command_capture(status_command, timeout=20)
    except Exception as e:
        log_error(f"Failed checking service status for {record['name']}: {e}")
        return False

    stdout = (result.stdout or "").strip().lower()
    stderr = (result.stderr or "").strip().lower()
    service_type = record.get("service_type")

    if service_type == "windows-service":
        return result.returncode == 0 and "running" in stdout

    if service_type == "systemd-user-service":
        return result.returncode == 0 and stdout == "active"

    if service_type == "launch-agent":
        return result.returncode == 0 and "could not find service" not in stdout and "could not find service" not in stderr

    return result.returncode == 0


def get_windows_service_state(record):
    identifier = record.get("identifier", "")
    if not identifier:
        return None

    escaped = identifier.replace("'", "''")
    command = [
        "powershell.exe",
        "-NoLogo",
        "-NoProfile",
        "-Command",
        (
            f"$svc = Get-Service -Name '{escaped}' -ErrorAction SilentlyContinue; "
            "if ($null -eq $svc) { exit 3 }; "
            "Write-Output $svc.Status"
        ),
    ]

    try:
        result = run_command_capture(command, timeout=20)
    except Exception as e:
        log_error(f"Failed reading Windows service state for {record['name']}: {e}")
        return None

    if result.returncode != 0:
        return None

    return (result.stdout or "").strip()


def get_process_candidate_paths(proc):
    candidate_paths = []

    exe_path = proc.info.get("exe")
    if exe_path:
        candidate_paths.append(exe_path)

    for item in proc.info.get("cmdline") or []:
        if item and os.path.isabs(item):
            candidate_paths.append(item)

    normalized_paths = []
    for path_value in candidate_paths:
        try:
            normalized_paths.append(normalize_fs_path(path_value))
        except Exception:
            continue

    return normalized_paths


def find_processes_by_targets(target_paths=None, target_roots=None, exclude_pids=None):
    exclude_pids = set(exclude_pids or [])
    normalized_targets = {normalize_fs_path(path_value) for path_value in (target_paths or []) if path_value}
    normalized_roots = [normalize_fs_path(root_path) for root_path in (target_roots or []) if root_path]
    matches = []

    for proc in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
        if proc.pid in exclude_pids:
            continue

        try:
            proc_paths = get_process_candidate_paths(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

        exact_match = any(proc_path in normalized_targets for proc_path in proc_paths)
        root_match = any(
            is_path_within_root(proc_path, root_path)
            for proc_path in proc_paths
            for root_path in normalized_roots
        )

        if exact_match or root_match:
            matches.append(proc)

    return matches


def find_processes_for_service_record(record, exclude_pids=None):
    target_paths = [record.get("binary_path"), record.get("runner_path")]
    return find_processes_by_targets(target_paths=target_paths, exclude_pids=exclude_pids)


def terminate_processes(processes, label, timeout=30):
    active_processes = []
    for proc in processes:
        try:
            if proc.is_running():
                active_processes.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if not active_processes:
        return

    process_ids = ", ".join(str(proc.pid) for proc in active_processes)
    log_info(f"Stopping {label} processes: {process_ids}")

    for proc in active_processes:
        try:
            proc.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    _, still_alive = psutil.wait_procs(active_processes, timeout=max(timeout / 2, 1))

    for proc in still_alive:
        try:
            proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    _, remaining = psutil.wait_procs(still_alive, timeout=max(timeout / 2, 1))
    if remaining:
        remaining_ids = ", ".join(str(proc.pid) for proc in remaining)
        raise RuntimeError(f"Unable to stop {label} processes: {remaining_ids}")


def invoke_service_action(record, action, timeout=30):
    if record.get("service_type") == "windows-service" and action in {"start", "stop"}:
        service_state = (get_windows_service_state(record) or "").lower()
        identifier = record.get("identifier") or record.get("name")

        if action == "stop" and service_state == "stopped":
            log_info(f"Service '{record['name']}' already stopped")
            return
        if action == "start" and service_state == "running":
            log_info(f"Service '{record['name']}' already running")
            return

        sc_action = "stop" if action == "stop" else "start"
        command = ["sc.exe", sc_action, identifier]
        log_info(f"Running Windows service action '{action}' for {record['name']}: {format_command(command)}")
        result = run_command_capture(command, timeout=timeout)

        stdout = truncate_for_log(result.stdout)
        stderr = truncate_for_log(result.stderr)
        if stdout:
            log_info(f"Windows service '{record['name']}' {action} stdout: {stdout}")
        if stderr:
            if result.returncode == 0:
                log_info(f"Windows service '{record['name']}' {action} stderr: {stderr}")
            else:
                log_error(f"Windows service '{record['name']}' {action} stderr: {stderr}")

        if result.returncode != 0:
            raise RuntimeError(
                f"Windows service action '{action}' failed for {record['name']} with exit code {result.returncode}"
            )
        return

    command = record.get("commands", {}).get(action)
    if not command:
        raise RuntimeError(f"Service manifest {record['manifest_path']} has no '{action}' command")

    log_info(f"Running service action '{action}' for {record['name']}: {format_command(command)}")
    result = run_command_capture(command, timeout=timeout)

    stdout = truncate_for_log(result.stdout)
    stderr = truncate_for_log(result.stderr)
    if stdout:
        log_info(f"Service '{record['name']}' {action} stdout: {stdout}")
    if stderr:
        if result.returncode == 0:
            log_info(f"Service '{record['name']}' {action} stderr: {stderr}")
        else:
            log_error(f"Service '{record['name']}' {action} stderr: {stderr}")

    if result.returncode != 0:
        raise RuntimeError(
            f"Service action '{action}' failed for {record['name']} with exit code {result.returncode}"
        )


def wait_for_pid_exit(pid, timeout=60):
    if not pid or pid == os.getpid():
        return

    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
                return
        except psutil.NoSuchProcess:
            return

        time.sleep(1)

    raise RuntimeError(f"Timed out waiting for process {pid} to exit")


def wait_for_service_transition(record, should_be_running, timeout=45, exclude_pids=None):
    deadline = time.time() + timeout
    exclude_pids = set(exclude_pids or [])
    service_type = record.get("service_type")
    last_logged_state = None
    last_progress_bucket = -1
    target_state_name = "running" if should_be_running else "stopped"

    log_info(
        f"Waiting for service '{record['name']}' to become {target_state_name} "
        f"(timeout={timeout}s)"
    )

    while time.time() < deadline:
        running = is_service_running(record)
        processes = find_processes_for_service_record(record, exclude_pids=exclude_pids)
        has_processes = bool(processes)
        elapsed_seconds = int(timeout - max(deadline - time.time(), 0))

        if service_type == "windows-service":
            current_state = get_windows_service_state(record) or "Unknown"
            if current_state != last_logged_state:
                log_info(f"Windows service '{record['name']}' current state: {current_state}")
                last_logged_state = current_state
            progress_bucket = elapsed_seconds // 5
            if progress_bucket != last_progress_bucket:
                log_info(
                    f"Service '{record['name']}' wait progress: elapsed={elapsed_seconds}s, "
                    f"target={target_state_name}, current_state={current_state}"
                )
                last_progress_bucket = progress_bucket
            if should_be_running and running:
                return
            if not should_be_running and not running:
                return
            time.sleep(1)
            continue

        if should_be_running:
            if service_type == "launch-agent":
                if has_processes or running:
                    return
            elif running or has_processes:
                return
        else:
            if service_type == "launch-agent":
                if not has_processes:
                    return
            elif not running and not has_processes:
                return

        time.sleep(1)

    state_name = "running" if should_be_running else "stopped"
    raise RuntimeError(f"Timed out waiting for service {record['name']} to become {state_name}")


def get_service_stop_priority(record):
    name_parts = " ".join(
        str(part).lower()
        for part in [record.get("name"), record.get("identifier"), record.get("manifest_name")]
        if part
    )

    if "tray" in name_parts:
        return 0
    if "updater" in name_parts:
        return 1
    if "slave" in name_parts:
        return 2
    return 3


def get_current_program_path():
    if getattr(sys, "frozen", False):
        return os.path.abspath(sys.executable)
    return os.path.abspath(__file__)


def current_program_is_managed_install(config):
    current_program_path = get_current_program_path()
    for root_path in [config.get("installPath"), config.get("serviceInstallPath")]:
        if root_path and is_path_within_root(current_program_path, root_path):
            return True

    return False


def inspect_runtime_state(config):
    service_records = discover_service_manifest_records(config)
    for record in service_records:
        record["was_running"] = is_service_running(record)
        state_label = "running" if record["was_running"] else "stopped"
        log_info(f"Service '{record['name']}' detected as {state_label}")

    dashboard_processes = find_processes_by_targets(
        target_roots=[os.path.join(config["installPath"], "dashboard_gui")],
        exclude_pids={os.getpid()},
    )
    dashboard_records = []
    if dashboard_processes:
        process_ids = ", ".join(str(proc.pid) for proc in dashboard_processes)
        log_info(f"Dashboard currently running with processes: {process_ids}")
        for proc in dashboard_processes:
            try:
                dashboard_records.append(
                    {
                        "pid": proc.pid,
                        "name": proc.info.get("name") or "",
                        "exe": proc.info.get("exe") or "",
                    }
                )
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

    return {
        "service_records": service_records,
        "dashboard_running": bool(dashboard_processes),
        "dashboard_processes": dashboard_records,
    }


def find_dashboard_processes_from_runtime_state(runtime_state, exclude_pids=None):
    dashboard_records = (runtime_state or {}).get("dashboard_processes") or []
    process_names = {record.get("name", "").lower() for record in dashboard_records if record.get("name")}
    exclude_pids = set(exclude_pids or [])
    matches = []

    if not process_names:
        return matches

    for proc in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
        if proc.pid in exclude_pids:
            continue
        try:
            process_name = (proc.info.get("name") or "").lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if process_name in process_names:
            matches.append(proc)

    return matches


def collect_dashboard_processes(config, runtime_state=None):
    dashboard_dir = os.path.join(config["installPath"], "dashboard_gui")
    processes = find_processes_by_targets(
        target_roots=[dashboard_dir],
        exclude_pids={os.getpid()},
    )

    if not processes and (runtime_state or {}).get("dashboard_running"):
        log_info("Dashboard was marked running before helper handoff; retrying by preserved process names")
        processes = find_dashboard_processes_from_runtime_state(runtime_state, exclude_pids={os.getpid()})

    return dashboard_dir, processes


def stop_dashboard_if_running(config, runtime_state=None):
    dashboard_dir, processes = collect_dashboard_processes(config, runtime_state=runtime_state)
    log_info(f"Checking dashboard processes under: {dashboard_dir}")

    if not processes:
        log_info("Dashboard is not running")
        return False

    dashboard_process_ids = ", ".join(str(proc.pid) for proc in processes)
    log_info(f"Dashboard candidate processes selected for stop: {dashboard_process_ids}")
    log_info("Dashboard is running; closing it before update")
    terminate_processes(processes, "dashboard", timeout=30)
    return True


def ensure_dashboard_stopped(config, runtime_state=None, max_attempts=3):
    dashboard_dir, processes = collect_dashboard_processes(config, runtime_state=runtime_state)
    was_running = bool(processes)

    if not was_running:
        log_info(f"Dashboard is not running under: {dashboard_dir}")
        return False

    for attempt in range(1, max_attempts + 1):
        process_ids = ", ".join(str(proc.pid) for proc in processes)
        log_info(
            f"Dashboard stop attempt {attempt}/{max_attempts} with candidate processes: {process_ids}"
        )
        terminate_processes(processes, "dashboard", timeout=30)

        _, processes = collect_dashboard_processes(config, runtime_state=runtime_state)
        if not processes:
            log_info("Dashboard confirmed stopped")
            return True

        remaining_ids = ", ".join(str(proc.pid) for proc in processes)
        log_error(f"Dashboard still running after stop attempt {attempt}: {remaining_ids}")

    raise RuntimeError("Dashboard remained running after all stop attempts")


def ensure_service_stopped(record, timeout=60, max_attempts=3):
    for attempt in range(1, max_attempts + 1):
        current_state = get_windows_service_state(record) if record.get("service_type") == "windows-service" else None
        log_info(
            f"Service '{record['name']}' stop verification attempt {attempt}/{max_attempts}"
            + (f" current_state={current_state}" if current_state else "")
        )

        try:
            wait_for_service_transition(record, should_be_running=False, timeout=timeout, exclude_pids={os.getpid()})
            log_info(f"Service '{record['name']}' stopped successfully")
            return
        except Exception as e:
            if attempt >= max_attempts:
                raise RuntimeError(
                    f"Service '{record['name']}' did not stop after {max_attempts} attempts: {e}"
                ) from e

            log_error(f"Service '{record['name']}' stop verification failed on attempt {attempt}: {e}")
            invoke_service_action(record, "stop", timeout=45)


def launch_dashboard_if_present(install_path):
    dashboard_dir = os.path.join(install_path, "dashboard_gui")
    if not os.path.isdir(dashboard_dir):
        log_info(f"Dashboard directory not found, skipping relaunch: {dashboard_dir}")
        return False

    try:
        if sys.platform.startswith("win"):
            for candidate_name in ["BreakEven.exe", "BreakEven Dashboard.exe"]:
                candidate_path = os.path.join(dashboard_dir, candidate_name)
                if os.path.exists(candidate_path):
                    spawn_detached([candidate_path], cwd=dashboard_dir)
                    log_info(f"Dashboard relaunched from {candidate_path}")
                    return True

            log_info(f"No Windows dashboard executable found in {dashboard_dir}")
            return False

        if sys.platform == "darwin":
            app_path = os.path.join(dashboard_dir, "BreakEven.app")
            if os.path.exists(app_path):
                spawn_detached(["open", app_path], cwd=dashboard_dir)
                log_info(f"Dashboard relaunched from {app_path}")
                return True

            dmg_path = os.path.join(dashboard_dir, "BreakEven.dmg")
            if os.path.exists(dmg_path):
                spawn_detached(["open", dmg_path], cwd=dashboard_dir)
                log_info(f"Dashboard disk image reopened from {dmg_path}")
                return True

            log_info(f"No macOS dashboard artifact found in {dashboard_dir}")
            return False

        if sys.platform.startswith("linux"):
            executable_candidates = [
                os.path.join(dashboard_dir, "BreakEven"),
                os.path.join(dashboard_dir, "BreakEven.AppImage"),
                os.path.join(dashboard_dir, "BreakEven-x86_64.AppImage"),
            ]
            for candidate_path in executable_candidates:
                if not os.path.exists(candidate_path):
                    continue

                try:
                    os.chmod(candidate_path, 0o755)
                except Exception:
                    pass

                spawn_detached([candidate_path], cwd=dashboard_dir)
                log_info(f"Dashboard relaunched from {candidate_path}")
                return True

            for package_name in ["BreakEven.deb", "BreakEven.rpm"]:
                package_path = os.path.join(dashboard_dir, package_name)
                if os.path.exists(package_path):
                    spawn_detached(["xdg-open", package_path], cwd=dashboard_dir)
                    log_info(f"Dashboard package reopened from {package_path}")
                    return True

            log_info(f"No Linux dashboard artifact found in {dashboard_dir}")
            return False

        log_info(f"Dashboard relaunch is not supported on platform {sys.platform}")
        return False
    except Exception as e:
        log_error(f"Failed to relaunch dashboard: {e}")
        return False


def get_helper_runtime_root(config):
    base_root = config.get("serviceInstallPath") or config.get("installPath") or RUNTIME_BASE_DIR
    runtime_root = os.path.join(base_root, "updater_runtime")
    os.makedirs(runtime_root, exist_ok=True)
    return runtime_root


def create_helper_copy(config):
    runtime_root = get_helper_runtime_root(config)
    helper_dir = os.path.join(
        runtime_root,
        f"helper_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{os.getpid()}"
    )
    os.makedirs(helper_dir, exist_ok=True)

    if getattr(sys, "frozen", False):
        source_path = sys.executable
        helper_name = os.path.basename(sys.executable)
    else:
        source_path = os.path.abspath(__file__)
        helper_name = os.path.basename(__file__)

    helper_path = os.path.join(helper_dir, helper_name)
    shutil.copy2(source_path, helper_path)

    if os.name != "nt":
        try:
            os.chmod(helper_path, 0o755)
        except Exception:
            pass

    return helper_dir, helper_path


def get_helper_marker_path(config=None, helper_state=None):
    if helper_state and helper_state.get("runtime_root"):
        runtime_root = helper_state["runtime_root"]
    elif config is not None:
        runtime_root = get_helper_runtime_root(config)
    else:
        runtime_root = os.path.join(RUNTIME_BASE_DIR, "updater_runtime")

    os.makedirs(runtime_root, exist_ok=True)
    return os.path.join(runtime_root, "updater_helper_pending.json")


def read_helper_marker(config=None, helper_state=None):
    marker_path = get_helper_marker_path(config=config, helper_state=helper_state)
    if not os.path.exists(marker_path):
        return None

    try:
        with open(marker_path, "r", encoding="utf-8") as f:
            marker = json.load(f)
        marker["marker_path"] = marker_path
        return marker
    except Exception as e:
        log_error(f"Failed reading helper marker {marker_path}: {e}")
        return None


def clear_helper_marker(config=None, helper_state=None):
    marker_path = get_helper_marker_path(config=config, helper_state=helper_state)
    if os.path.exists(marker_path):
        try:
            os.remove(marker_path)
        except Exception as e:
            log_error(f"Failed removing helper marker {marker_path}: {e}")


def write_helper_marker(marker, config=None, helper_state=None):
    marker_path = get_helper_marker_path(config=config, helper_state=helper_state)
    os.makedirs(os.path.dirname(marker_path), exist_ok=True)
    with open(marker_path, "w", encoding="utf-8") as f:
        json.dump(marker, f)


def is_process_alive(pid):
    if not pid:
        return False

    try:
        proc = psutil.Process(pid)
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def helper_marker_is_active(marker, max_age_seconds=300):
    if not marker:
        return False

    state_path = marker.get("state_path")
    helper_pid = marker.get("helper_pid")
    created_ts = marker.get("created_ts")

    if helper_pid and is_process_alive(helper_pid):
        return True

    if state_path and os.path.exists(state_path):
        if created_ts is None:
            return True
        return (time.time() - created_ts) <= max_age_seconds

    return False


def delete_windows_task(task_name):
    if not task_name or os.name != "nt":
        return

    try:
        run_command_capture(["schtasks.exe", "/Delete", "/F", "/TN", task_name], timeout=20)
    except Exception as e:
        log_error(f"Failed deleting helper task {task_name}: {e}")


def launch_update_helper(manifest, local_version, latest_version, os_type, config, runtime_state):
    helper_dir, helper_path = create_helper_copy(config)
    runtime_root = get_helper_runtime_root(config)
    state_path = os.path.join(helper_dir, "update_state.json")
    bootstrap_log_path = os.path.join(helper_dir, "helper_bootstrap.log")
    state = {
        "manifest": manifest,
        "local_version": local_version,
        "latest_version": latest_version,
        "os_type": os_type,
        "original_pid": os.getpid(),
        "launched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "helper_dir": helper_dir,
        "runtime_root": runtime_root,
        "bootstrap_log_path": bootstrap_log_path,
        "helper_task_name": None,
        "runtime_state": runtime_state,
    }

    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f)

    if getattr(sys, "frozen", False):
        command = [helper_path, HELPER_STATE_ARG, state_path]
    else:
        command = [sys.executable, helper_path, HELPER_STATE_ARG, state_path]

    helper_env = os.environ.copy()
    helper_env[RUNTIME_BASE_ENV] = RUNTIME_BASE_DIR
    helper_env[HELPER_STATE_ENV] = state_path
    helper_env[HELPER_BOOTSTRAP_LOG_ENV] = bootstrap_log_path
    write_bootstrap_trace(f"Preparing helper launch from {helper_path}")
    append_helper_bootstrap_log(bootstrap_log_path, f"Parent preparing helper launch | state={state_path}")
    helper_launch = launch_helper_process(command, helper_dir, helper_env)
    state["helper_task_name"] = helper_launch.get("task_name")
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f)

    startup_ok, startup_trace = wait_for_helper_startup(bootstrap_log_path, timeout=45)
    if startup_ok:
        log_info("Helper launch verified by bootstrap trace")
        delete_windows_task(helper_launch.get("task_name"))
    else:
        trace_excerpt = truncate_for_log(startup_trace or "<no bootstrap trace>", limit=2000)
        log_error(
            "Helper launch could not be verified within 45 seconds. "
            f"Bootstrap trace: {trace_excerpt}"
        )
        raise RuntimeError("Helper launch verification failed")

    write_helper_marker({
        "state_path": state_path,
        "helper_path": helper_path,
        "helper_pid": helper_launch.get("pid"),
        "helper_task_name": helper_launch.get("task_name"),
        "runtime_root": runtime_root,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "created_ts": time.time(),
        "local_version": local_version,
        "latest_version": latest_version,
    }, config=config)

    log_info(
        f"Launched detached updater helper from {helper_path} "
        f"for update {local_version} -> {latest_version}"
        + (
            f" with pid {helper_launch['pid']}"
            if helper_launch.get("pid")
            else f" using task {helper_launch['task_name']}"
        )
    )
    log_info(f"Helper bootstrap trace path: {bootstrap_log_path}")


def perform_coordinated_update(os_type, manifest, config, helper_state=None, runtime_state=None):
    runtime_state = runtime_state or inspect_runtime_state(config)
    running_services = [record for record in runtime_state["service_records"] if record.get("was_running")]
    running_services.sort(key=get_service_stop_priority)

    services_to_restart = []
    dashboard_was_running = False

    try:
        if running_services:
            log_info(f"Stopping {len(running_services)} managed service(s) before update")
            ordered_names = ", ".join(record["name"] for record in running_services)
            log_info(f"Managed service stop order: {ordered_names}")
        else:
            log_info("No managed services were marked running before update")

        for index, record in enumerate(running_services, start=1):
            log_info(f"Service stop signal phase {index}/{len(running_services)} starting for '{record['name']}'")
            invoke_service_action(record, "stop", timeout=45)
            services_to_restart.append(record)
            log_info(f"Service '{record['name']}' stop command issued")

        log_info("All managed service stop commands issued; proceeding to dashboard shutdown")
        dashboard_was_running = ensure_dashboard_stopped(config, runtime_state=runtime_state, max_attempts=3)
        log_info(f"Dashboard shutdown phase complete; dashboard_was_running={dashboard_was_running}")

        for index, record in enumerate(services_to_restart, start=1):
            log_info(f"Service stop wait phase {index}/{len(services_to_restart)} waiting for '{record['name']}'")
            ensure_service_stopped(record, timeout=60, max_attempts=3)

        if helper_state and helper_state.get("original_pid"):
            log_info(f"Waiting for original updater process {helper_state['original_pid']} to exit")
            wait_for_pid_exit(helper_state["original_pid"], timeout=60)

        log_info("Beginning install/update phase after service and dashboard shutdown")
        return install_update(os_type, manifest)

    finally:
        for record in reversed(services_to_restart):
            try:
                invoke_service_action(record, "start", timeout=45)
                wait_for_service_transition(record, should_be_running=True, timeout=60, exclude_pids={os.getpid()})
                log_info(f"Service '{record['name']}' restarted successfully")
            except Exception as restart_error:
                log_error(f"Failed to restart service '{record['name']}': {restart_error}")

        if dashboard_was_running:
            launch_dashboard_if_present(config["installPath"])


def run_helper_update_job(state_path):
    write_bootstrap_trace(f"Entered helper mode with state file: {state_path}")
    with open(state_path, "r", encoding="utf-8") as f:
        helper_state = json.load(f)

    write_bootstrap_trace("Helper state file loaded successfully")
    log_info(f"Updater helper started using state file: {state_path}")
    log_info(
        f"Helper runtime paths | runtime_base={RUNTIME_BASE_DIR} | "
        f"config_path={CONFIG_PATH} | log_path={LOG_PATH}"
    )
    try:
        run_update_cycle(
            manifest=helper_state.get("manifest"),
            helper_mode=True,
            helper_state=helper_state,
        )
    finally:
        write_bootstrap_trace("Helper mode finishing")
        delete_windows_task(helper_state.get("helper_task_name"))
        clear_helper_marker(helper_state=helper_state)
        try:
            os.remove(state_path)
        except Exception:
            pass


# In[ ]:


def install_update(os_type, manifest):
    config = get_config()
    install_path = config["installPath"]
    service_path = config.get("serviceInstallPath")

    if "stable" not in manifest or os_type not in manifest["stable"]:
        raise RuntimeError(f"Manifest does not contain stable update info for OS: {os_type}")

    stable_info = manifest["stable"][os_type]
    version_path = stable_info["path"]
    components = stable_info["components"]

    log_info(f"Installing update for OS={os_type}, version_path={version_path}")

    downloaded_files = []

    for component_name, component_data in components.items():
        log_info(f"Processing component: {component_name}")

        for file_info in component_data.get("files", []):
            rel_path = file_info["relative_path"]
            expected_sha256 = file_info["sha256"]

            url = resolve_file_download_url(manifest, version_path, file_info)
            dest = os.path.join(install_path, *rel_path.split("/"))

            download_file_verified(url, dest, expected_sha256)
            downloaded_files.append(dest)

    latest_version = manifest.get("stable_version") or stable_info.get("version")

    config_paths_to_update = [os.path.join(install_path, "client_config.json")]

    if "client_service" in components and service_path:
        log_info("Updating client_service files at serviceInstallPath")
    
        service_client_dir = os.path.join(service_path, "client_service")
        os.makedirs(service_client_dir, exist_ok=True)
    
        for file_info in components["client_service"].get("files", []):
            rel_path = file_info["relative_path"].replace("\\", "/").lstrip("/")
    
            # Only sync files inside client_service
            if not rel_path.startswith("client_service/"):
                continue
    
            rel_inside_service = rel_path[len("client_service/"):]
            src_file = os.path.join(install_path, *rel_path.split("/"))
            dst_file = os.path.join(service_client_dir, *rel_inside_service.split("/"))
    
            if not os.path.exists(src_file):
                raise RuntimeError(f"Source file missing for service sync: {src_file}")
    
            os.makedirs(os.path.dirname(dst_file), exist_ok=True)
    
            temp_dst = dst_file + ".download"
            shutil.copy2(src_file, temp_dst)
    
            if os.path.exists(dst_file):
                os.remove(dst_file)
    
            os.replace(temp_dst, dst_file)
    
            log_info(f"Replaced service file -> {dst_file}")

            config_paths_to_update.append(os.path.join(service_path, "client_config.json"))

        for config_path in dict.fromkeys(config_paths_to_update):
            update_config_version_at_path(config_path, latest_version)
        
    return downloaded_files


# In[ ]:


def run_update_cycle(manifest=None, helper_mode=False, helper_state=None):
    try:
        config = get_config()

        if not helper_mode:
            helper_marker = read_helper_marker(config=config)
            if helper_marker and helper_marker_is_active(helper_marker):
                helper_pid = helper_marker.get("helper_pid")
                log_info(
                    "Update helper is already in progress"
                    + (f" with pid {helper_pid}" if helper_pid else "")
                    + "; skipping this cycle"
                )
                return manifest

            if helper_marker:
                log_info("Found stale helper marker; clearing it and continuing")
                clear_helper_marker(config=config)

        if not config.get("autoUpdate", True):
            log_info("Auto update disabled in client_config.json")
            return manifest

        local_version = config["version"]
        os_type = get_os_type()

        # Only log these details during real update activity or errors
        #log_info(f"Local Version: {local_version}")
        #log_info(f"OS Type: {os_type}")

        if manifest is None:
            manifest = fetch_manifest()

        if not manifest:
            return None

        latest_version = manifest.get("stable_version") or manifest["stable"][os_type]["version"]

        if not is_update_available(local_version, latest_version):
            log_up_to_date_once_per_day(
                f"Client already up to date. local={local_version}, latest={latest_version}, os={os_type}"
            )
            return manifest
        
        log_info(f"Local Version: {local_version}")
        log_info(f"OS Type: {os_type}")
        log_info(f"Latest Version: {latest_version}")

        runtime_state = helper_state.get("runtime_state") if helper_mode and helper_state else None
        if runtime_state:
            log_info("Using preserved runtime state captured before helper handoff")
        else:
            runtime_state = inspect_runtime_state(config)
        if not helper_mode and (
            current_program_is_managed_install(config)
            or any(record.get("is_updater") and record.get("was_running") for record in runtime_state["service_records"])
        ):
            log_info("Managed updater runtime detected; handing off update work to detached helper")
            launch_update_helper(manifest, local_version, latest_version, os_type, config, runtime_state)
            raise SystemExit(0)

        start_time = datetime.now()
        log_info(f"===== UPDATE STARTED | from={local_version} to={latest_version} =====")

        downloaded_files = perform_coordinated_update(
            os_type,
            manifest,
            config,
            helper_state=helper_state,
            runtime_state=runtime_state,
        )
        
        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()

        log_info(f"Downloaded and verified files count: {len(downloaded_files)}")
        for file_path in downloaded_files:
            log_info(f"Updated file: {file_path}")

        log_info(
            f"===== UPDATE SUCCESS | from={local_version} to={latest_version} | "
            f"started={start_time.strftime('%Y-%m-%d %H:%M:%S')} | "
            f"ended={end_time.strftime('%Y-%m-%d %H:%M:%S')} | "
            f"duration_seconds={duration:.2f} ====="
        )

        return manifest

    except Exception as e:
        log_error(f"[Updater Error] {e}")
        return manifest


# In[ ]:


if __name__ == "__main__":
    helper_state_path = parse_helper_state_path()
    write_bootstrap_trace(
        "Process entrypoint reached"
        + (f" with helper state {helper_state_path}" if helper_state_path else " in service mode")
    )

    if helper_state_path:
        run_helper_update_job(helper_state_path)
    else:
        log_info("Updater service started")

        while True:
            manifest = fetch_manifest()
            manifest = run_update_cycle(manifest)

            if manifest:
                interval = manifest.get("check_interval_seconds", CHECK_INTERVAL)
            else:
                interval = CHECK_INTERVAL

            # do not log sleep every cycle; it creates unnecessary noise
            #log_info(f"Sleeping for {interval} seconds before next update check")
            
            time.sleep(interval)


# In[ ]:




