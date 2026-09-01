# -*- coding: utf-8 -*-
"""Standalone Stripchat Live Recorder bot — config (env only)."""
import os

ROOT = os.path.dirname(os.path.abspath(__file__))


def _int(name, default=0):
    v = os.environ.get(name)
    if v is None or str(v).strip() == "":
        return default
    try:
        return int(str(v).strip())
    except Exception:
        return default


def _str(name, default=""):
    v = os.environ.get(name)
    return v if v else default


def _bool(name, default=False):
    v = (os.environ.get(name) or "").strip().lower()
    if not v:
        return default
    if v in ("1", "true", "yes", "y", "on"):
        return True
    if v in ("0", "false", "no", "n", "off"):
        return False
    return default


# Telegram — apna ALAG bot token (BIMBO wala mat dalna)
BOT_TOKEN = (
    _str("SC_BOT_TOKEN") or _str("BOT_TOKEN") or _str("TELEGRAM_BOT_TOKEN")
    or _str("TG_BOT_TOKEN") or _str("BIMBO_BOT_TOKEN")
)
API_ID = _int("SC_API_ID") or _int("API_ID") or _int("TG_API_ID") or _int("BIMBO_API_ID")
API_HASH = _str("SC_API_HASH") or _str("API_HASH") or _str("TG_API_HASH") or _str("BIMBO_API_HASH")
SESSION_NAME = _str("SC_SESSION_NAME", "stripchat_live_bot")

OWNER_ID = _int("SC_OWNER_ID") or _int("OWNER_ID")
_admins = _str("SC_ADMIN_IDS") or _str("ADMIN_IDS")
ADMIN_IDS = set()
if _admins:
    for x in _admins.split():
        try:
            ADMIN_IDS.add(int(x))
        except ValueError:
            pass
if OWNER_ID:
    ADMIN_IDS.add(OWNER_ID)

_allowed = _str("SC_ALLOWED_USERS") or _str("ALLOWED_USERS")
ALLOWED_USERS = set()
if _allowed:
    for x in _allowed.split():
        try:
            ALLOWED_USERS.add(int(x))
        except ValueError:
            pass

# true = koi bhi user record kar sakta (bandwidth heavy)
ALLOW_ALL = _bool("SC_ALLOW_ALL", False)
# OWNER_ID na ho to silent-dead bot na bane — pehle deploy pe reply aaye
if not OWNER_ID and not ALLOWED_USERS and not ADMIN_IDS:
    ALLOW_ALL = True

DOWNLOAD_DIR = _str("SC_DOWNLOAD_DIR") or os.path.join(ROOT, "downloads")
KEY_FILE = _str("STRIPCHAT_KEY_FILE") or os.path.join(ROOT, "keys.txt")

MAX_CONCURRENT_REC = max(1, _int("SC_MAX_REC", 3))
MAX_REC_PER_USER = 1
PART_MAX_BYTES = _int("SC_PART_MAX", 1850 * 1024 * 1024)
UNTIL_STOP_CAP_MIN = max(5, _int("SC_UNTIL_STOP_CAP_MIN", 180))
PLAYLIST_POLL = float(_str("SC_PLAYLIST_POLL", "2.0") or "2.0")
EDIT_EVERY = 8.0
MAX_PLAYLIST_FAILS = 8
WORKERS = max(1, _int("SC_WORKERS", 4))
PORT = _int("PORT", 8080)
LOG_CHANNEL = _int("SC_LOG_CHANNEL", 0)
AUTO_CLEANUP_HOURS = _int("SC_AUTO_CLEANUP_HOURS", 6)
