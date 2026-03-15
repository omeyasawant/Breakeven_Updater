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

LAST_UP_TO_DATE_LOG_DATE = None

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
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
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

    install_config_path = os.path.join(install_path, "client_config.json")
    update_config_version_at_path(install_config_path, latest_version)

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

        service_config_path = os.path.join(service_path, "client_config.json")
        update_config_version_at_path(service_config_path, latest_version)
        
    return downloaded_files


# In[ ]:


def run_update_cycle(manifest=None):
    try:
        config = get_config()

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

        start_time = datetime.now()
        log_info(f"===== UPDATE STARTED | from={local_version} to={latest_version} =====")

        downloaded_files = install_update(os_type, manifest)
        
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




