# -*- coding: utf-8 -*-
"""Auto-monitor: max 2 models. Fast HLS ping, no TG flood, crash-proof loop."""
from __future__ import annotations

import os
import gc
import json
import time
import asyncio
import logging
from typing import Dict, List, Optional

import aiohttp

import config

logger = logging.getLogger("sc.monitor")

STORE = os.path.join(config.ROOT, "monitors.json")

_MON: Dict[str, List[dict]] = {}
_LOOP_STARTED = False
_SESSION: Optional[aiohttp.ClientSession] = None
_ID_FAIL: Dict[str, float] = {}  # "uid:model" -> last full-resolve ts


def _norm(name: str) -> str:
    return (name or "").strip()


def _reclaim() -> None:
    try:
        gc.collect()
    except Exception:
        pass


def load() -> None:
    global _MON
    try:
        if os.path.exists(STORE):
            with open(STORE, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                _MON = {str(k): list(v) for k, v in data.items() if isinstance(v, list)}
    except Exception as e:
        logger.warning("monitor load: %s", e)
        _MON = {}


def save() -> None:
    try:
        tmp = STORE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_MON, f, ensure_ascii=False, indent=0)
        os.replace(tmp, STORE)
    except Exception as e:
        logger.warning("monitor save: %s", e)


def slots(uid: int) -> List[dict]:
    return list(_MON.get(str(uid), []))


def slot_models(uid: int) -> List[str]:
    return [s.get("model", "") for s in slots(uid) if s.get("model")]


def has(uid: int, model: str) -> bool:
    m = _norm(model).lower()
    return any((s.get("model") or "").lower() == m for s in slots(uid))


def add(uid: int, model: str, quality: str = "source") -> tuple[bool, str]:
    model = _norm(model)
    if not model:
        return False, "Model name empty."
    cur = _MON.setdefault(str(uid), [])
    if has(uid, model):
        return False, f"`{model}` pehle se monitor pe hai."
    if len(cur) >= config.MAX_MONITORS:
        return False, (
            f"Max **{config.MAX_MONITORS}** models. Pehle `/unmon Name` se slot khali karo."
        )
    cur.append({
        "model": model,
        "quality": (quality or "source").lower().replace(" ", "") or "source",
        "added": int(time.time()),
        "last_end": 0,
        "hits": 0,
        "last_state": "wait",
        "id": 0,
        "last_fail_tg": 0,
    })
    save()
    return True, f"📌 `{model}` monitor slot {len(cur)}/{config.MAX_MONITORS}"


def remove(uid: int, model: str) -> bool:
    m = _norm(model).lower()
    cur = _MON.get(str(uid), [])
    nxt = [s for s in cur if (s.get("model") or "").lower() != m]
    if len(nxt) == len(cur):
        return False
    if nxt:
        _MON[str(uid)] = nxt
    else:
        _MON.pop(str(uid), None)
    save()
    _reclaim()
    return True


def clear(uid: int) -> int:
    n = len(_MON.pop(str(uid), []) or [])
    if n:
        save()
    _reclaim()
    return n


def touch_end(uid: int, model: str) -> None:
    m = _norm(model).lower()
    for s in _MON.get(str(uid), []):
        if (s.get("model") or "").lower() == m:
            s["last_end"] = time.time()
            s["last_state"] = "wait"
            save()
            _reclaim()
            return


def touch_hit(uid: int, model: str) -> None:
    m = _norm(model).lower()
    for s in _MON.get(str(uid), []):
        if (s.get("model") or "").lower() == m:
            s["hits"] = int(s.get("hits") or 0) + 1
            s["last_state"] = "live"
            save()
            return


def quality_for(uid: int, model: str) -> str:
    m = _norm(model).lower()
    for s in _MON.get(str(uid), []):
        if (s.get("model") or "").lower() == m:
            return s.get("quality") or "source"
    return "source"


def all_watchers() -> List[tuple[int, dict]]:
    out = []
    for uid, lst in list(_MON.items()):
        try:
            u = int(uid)
        except Exception:
            continue
        for s in lst:
            if s.get("model"):
                out.append((u, s))
    return out


async def _session() -> aiohttp.ClientSession:
    global _SESSION
    if _SESSION is None or _SESSION.closed:
        _SESSION = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=8, connect=3, sock_read=6),
            connector=aiohttp.TCPConnector(
                limit=10, ttl_dns_cache=300, enable_cleanup_closed=True, ssl=False,
            ),
        )
    return _SESSION


async def _close_session() -> None:
    global _SESSION
    s = _SESSION
    _SESSION = None
    if s is not None and not s.closed:
        try:
            await s.close()
        except Exception:
            pass
    _reclaim()


async def run_loop(client) -> None:
    """Never returns unless cancelled. Restarts inner tick forever."""
    global _LOOP_STARTED
    if _LOOP_STARTED:
        return
    _LOOP_STARTED = True
    load()
    logger.info("monitor loop on | slots=%s poll=%ss",
                sum(len(v) for v in _MON.values()), config.MONITOR_POLL)
    await asyncio.sleep(3)
    while True:
        try:
            await _tick(client)
        except asyncio.CancelledError:
            await _close_session()
            _LOOP_STARTED = False
            raise
        except Exception:
            logger.exception("monitor tick")
            try:
                await _close_session()
            except Exception:
                pass
            _reclaim()
        await asyncio.sleep(config.MONITOR_POLL)


async def _ensure_id(session, slot: dict) -> int:
    from engine import fetch_model_status

    mid = int(slot.get("id") or 0)
    if mid:
        return mid
    model = slot.get("model") or ""
    key = model.lower()
    now = time.time()
    if now - float(_ID_FAIL.get(key, 0) or 0) < 20:
        return 0
    try:
        st = await fetch_model_status(session, model)
        mid = int(st.get("id") or 0)
        if mid:
            slot["id"] = mid
            save()
            return mid
    except Exception as e:
        logger.debug("resolve id %s: %s", model, e)
    _ID_FAIL[key] = now
    return 0


async def _one(client, session, uid: int, slot: dict) -> None:
    from engine import is_hls_live
    from handlers import begin_recording, user_recording_model, user_rec_count

    model = slot.get("model") or ""
    if not model:
        return
    try:
        if user_recording_model(uid, model):
            slot["last_state"] = "rec"
            return
        last_end = float(slot.get("last_end") or 0)
        if last_end and (time.time() - last_end) < config.MONITOR_COOLDOWN:
            return
        if user_rec_count(uid) >= config.MAX_REC_PER_USER:
            return

        mid = await _ensure_id(session, slot)
        if not mid:
            return
        try:
            online = await is_hls_live(session, mid, timeout=1.6)
        except Exception as e:
            logger.debug("hls ping %s: %s", model, e)
            return
        if not online:
            if slot.get("last_state") not in ("wait", None):
                slot["last_state"] = "wait"
            return

        q = slot.get("quality") or "source"
        touch_hit(uid, model)
        logger.info("monitor AUTO-REC uid=%s model=%s id=%s hit=%s",
                    uid, model, mid, slot.get("hits"))
        ok, err = await begin_recording(
            client, uid, model, 0, q, from_monitor=True,
        )
        if not ok:
            logger.info("monitor rec skip %s: %s", model, err)
            slot["last_state"] = "wait"
            now = time.time()
            if now - float(slot.get("last_fail_tg") or 0) > 180:
                slot["last_fail_tg"] = now
                try:
                    from handlers import safe_send
                    await safe_send(
                        client, uid,
                        f"⚠️ `{model}` online thi, rec skip: {err}",
                        flood_sleep=False,
                    )
                except Exception:
                    pass
    except Exception:
        logger.exception("monitor one %s", model)


async def _tick(client) -> None:
    watchers = all_watchers()
    if not watchers:
        await _close_session()
        return
    session = await _session()
    await asyncio.gather(
        *[_one(client, session, uid, slot) for uid, slot in watchers],
        return_exceptions=True,
    )
