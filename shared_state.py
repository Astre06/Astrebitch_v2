# ================================================================
# 🧩 Shared State Module
# ================================================================

user_busy = {}

# ================================================================
# 🔧 Proxy Format Parser (shared by proxy_manager & proxy_check)
# ================================================================
import re

def parse_proxy_line(line: str):
    """Parses proxies in common formats and returns a dict or None if invalid."""
    if not line:
        return None

    line = line.strip().replace(" ", "")

    # Try multiple proxy patterns
    patterns = [
        # host:port:user:pass
        r"^([\w\.-]+):(\d{2,6}):([^:@]+):(.+)$",
        # user:pass@host:port
        r"^([^:@]+):([^:@]+)@([\w\.-]+):(\d{2,6})$",
        # user:pass:host:port
        r"^([^:@]+):([^:@]+):([\w\.-]+):(\d{2,6})$",
        # host:port@user:pass
        r"^([\w\.-]+):(\d{2,6})@([^:@]+):([^:@]+)$",
        # host:port (no auth)
        r"^([\w\.-]+):(\d{2,6})$",
    ]

    for p in patterns:
        m = re.match(p, line)
        if m:
            g = m.groups()
            if len(g) == 2:
                host, port = g
                return {"host": host, "port": int(port)}
            elif len(g) == 4:
                # Try to figure out which pattern matched
                if "@" in line or line.count(":") > 2:
                    # If it's host:port:user:pass
                    if g[0].replace(".", "").isalpha() or g[0].count(".") >= 1:
                        return {"host": g[0], "port": int(g[1]), "user": g[2], "pass": g[3]}
                    # Else maybe user:pass@host:port
                    elif g[2].replace(".", "").isalpha() or g[2].count(".") >= 1:
                        return {"host": g[2], "port": int(g[3]), "user": g[0], "pass": g[1]}
    return None

# ============================================================
# 🧾 Shared Function — Save Live CC JSON (per user & worker)
# ============================================================

import os
import json
import threading
import logging
from datetime import datetime

_livecc_folder_lock = threading.Lock()

def save_live_cc_to_json(user_id: str, worker_id: int, live_data: dict):
    """
    Thread-safe shared function.
    Each worker writes to its own live file:
        live-cc/<user_id>/Live_cc_<user_id>_<worker_id>.json
    """
    folder = os.path.join("live-cc", str(user_id))

    # Ensure per-user folder exists safely
    with _livecc_folder_lock:
        try:
            os.makedirs(folder, exist_ok=True)
        except Exception as e:
            logging.warning(f"[LIVE JSON] Failed to create folder {folder}: {e}")
            return

    file_path = os.path.join(folder, f"Live_cc_{user_id}_{worker_id}.json")

    # Add timestamp
    live_data["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        # Each worker writes to its own file (no shared writes)
        if os.path.exists(file_path):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
            except Exception:
                existing = []
        else:
            existing = []

        existing.append(live_data)

        # Write atomically with .tmp → replace
        tmp_path = f"{file_path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, file_path)

        logging.info(f"[LIVE JSON] Worker {worker_id} → {file_path}")
    except Exception as e:
        logging.error(f"[LIVE JSON ERROR] User {user_id}, Worker {worker_id}: {e}")
# ================================================================
# 🔁 Shared Function — Retry logic for site checks (Manual + Mass)
# ================================================================
def try_process_with_retries(card_data, chat_id, user_proxy=None, worker_id=None, max_tries=None):
    from site_auth_manager import remove_user_site, _load_state, process_card_for_user_sites

    tries = 0
    site_url, result = None, None

    # 🧩 Load once at start, cache sites in memory
    try:
        state = _load_state(chat_id)
        user_sites = list(state.get(str(chat_id), {}).get("sites", {}).keys())
    except Exception:
        user_sites = []

    if not user_sites:
        return None, {"status": "DECLINED", "reason": "No sites configured", "site_dead": True}

    max_tries = max_tries or len(user_sites)
    dead_sites = []
    last_reason = None

    while tries < max_tries and user_sites:
        site_url = user_sites[0]  # always use first available site
        print(f"[TRY] ({tries+1}/{max_tries}) Using site: {site_url}")

        site_url, result = process_card_for_user_sites(
            card_data,
            chat_id,
            proxy=user_proxy,
            worker_id=worker_id,
        )
        tries += 1

        # Normalize result
        if not isinstance(result, dict):
            result = {"status": "DECLINED", "reason": str(result or "Invalid result")}

        reason = (result.get("reason") or "").lower()
        last_reason = reason

        # 🧨 Detect real site failure
        if (
            result.get("site_dead")
            or "site response failed" in reason
            or ("request failed" in reason and "stripe" in reason)
            or "timeout" in reason
        ):
            print(f"[AUTO] Marking site as dead (retry next): {site_url}")
            dead_sites.append(site_url)
            if site_url in user_sites:
                user_sites.remove(site_url)
            continue  # retry next available site

        # ✅ Stop retrying — site responded (even if declined)
        print(f"[SUCCESS] Site responded successfully: {site_url}")
        break

    # 🧹 Clean up dead sites permanently
    for s in dead_sites:
        try:
            removed = remove_user_site(chat_id, s)
            if removed:
                print(f"[AUTO] Permanently removed dead site: {s}")
        except Exception as e:
            print(f"[AUTO] Error removing site {s}: {e}")

    # 🧠 If no sites left AND we never got a valid response
    if not user_sites and (not result or result.get("site_dead")):
        print("[FAIL] All sites failed or removed.")
        return None, {
            "status": "DECLINED",
            "reason": "All sites failed or removed",
            "site_dead": True,
        }

    # ✅ Return working site and result
    return site_url, result
