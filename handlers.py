# -*- coding: utf-8 -*-
"""Telegram UI — Stripchat live recorder only."""
from __future__ import annotations

import os
import re
import time
import asyncio
import logging
import secrets

import aiohttp
from pyrogram import Client, filters
from pyrogram.handlers import MessageHandler, CallbackQueryHandler
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait, MessageNotModified
from pyrogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
)

import config
import monitor
import panel
from engine import (
    TAGS, MouflonError, model_from_input, is_stripchat_url,
    fetch_model_status, fetch_online_models, load_key_map, load_sc_cookie,
    fetch_master, parse_variants,
    record_to_parts, remux_to_mp4, probe_video, make_thumb,
    cleanup_dir, humanbytes, fmt_dur,
)

logger = logging.getLogger("sc.handlers")

_RECS = {}
_USER_ACTIVE = {}
_USER_RECS = {}  # uid -> set(rec_id)
_START_LOCK = asyncio.Lock()
_UPLOAD_LOCK = asyncio.Lock()
_REMUX_LOCK = asyncio.Lock()
_Q = []  # only if SC_MAX_REC set
_UPLOADING = None  # model name currently uploading


async def safe_send(client, chat_id, text, flood_sleep=True, **kw):
    """Telegram send — FloodWait pe rec loop block nahi (flood_sleep=False)."""
    tries = 4 if flood_sleep else 1
    last = None
    for i in range(tries):
        try:
            return await client.send_message(chat_id, text, **kw)
        except FloodWait as e:
            last = e
            w = int(getattr(e, "value", 5) or 5)
            logger.warning("FloodWait send %ss", w)
            if not flood_sleep:
                return None
            await asyncio.sleep(min(w, 40) + 1)
        except Exception as e:
            last = e
            logger.debug("safe_send: %s", e)
            return None
    return None


async def safe_edit(msg, text, flood_sleep=False, **kw):
    try:
        return await msg.edit_text(text, **kw)
    except MessageNotModified:
        return None
    except FloodWait as e:
        w = int(getattr(e, "value", 5) or 5)
        logger.warning("FloodWait edit %ss (skip, rec continue)", w)
        if flood_sleep:
            await asyncio.sleep(min(w, 20) + 1)
            try:
                return await msg.edit_text(text, **kw)
            except Exception:
                return None
        return None
    except Exception:
        return None


def vanish(msg, sec: float = 12):
    """Bot ke junk replies 12s me udao — rec/video/panel nahi."""
    if not msg:
        return msg

    async def _gone():
        try:
            await asyncio.sleep(sec)
            await msg.delete()
        except Exception:
            pass

    try:
        asyncio.create_task(_gone())
    except Exception:
        pass
    return msg


def queue_list(uid: int = None):
    if uid is None:
        return list(_Q)
    return [x for x in _Q if x.get("uid") == uid]


def dash_text(uid: int) -> str:
    recs = []
    for rid in list(_USER_RECS.get(uid) or []):
        rec = _RECS.get(rid)
        if not rec:
            continue
        el = int(time.time() - rec.get("t0", time.time()))
        recs.append(f"│ 🔴 <b>{rec.get('model')}</b>  ⏱ {fmt_dur(el)}")
    q = queue_list(uid)
    mons = monitor.slots(uid)
    recn = sum(1 for s in mons if (s.get("last_state") or "") in ("rec", "live") or user_recording_model(uid, s.get("model") or ""))
    cap = int(config.MAX_REC_PER_USER or 0)
    rec_bit = f"{len(recs)}/{cap}" if cap else str(len(recs))
    lines = [
        "╭─ ⟨ <b>ＤＡＳＨ</b> ⟩ ─╮",
        f"│ rec <b>{rec_bit}</b>  q <b>{len(q)}</b>  mon <b>{len(mons)}</b>/{config.MAX_MONITORS}",
    ]
    if _UPLOADING:
        lines.append(f"│ 📤 upload <b>{_UPLOADING}</b>")
    if recs:
        lines.extend(recs[:12])
    else:
        lines.append("│ 🎙 rec idle")
    if q:
        names = ", ".join((x.get("model") or "?")[:16] for x in q[:8])
        lines.append(f"│ ⏳ queue: {names}")
    lines.append("╰─ unlimited rec · safe I/O ─╯")
    return "\n".join(lines)


def user_rec_count(uid: int) -> int:
    live = {r for r in (_USER_RECS.get(uid) or set()) if r in _RECS}
    if uid in _USER_RECS and live != _USER_RECS.get(uid):
        if live:
            _USER_RECS[uid] = live
        else:
            _USER_RECS.pop(uid, None)
    return len(live)


def user_recording_model(uid: int, model: str) -> bool:
    m = (model or "").lower()
    for rid in list(_USER_RECS.get(uid) or []):
        rec = _RECS.get(rid)
        if rec and (rec.get("model") or "").lower() == m:
            return True
    return False


def _track_add(uid: int, rec_id: str):
    _USER_RECS.setdefault(uid, set()).add(rec_id)
    _USER_ACTIVE[uid] = rec_id


def _track_del(uid: int, rec_id: str):
    s = _USER_RECS.get(uid)
    if s:
        s.discard(rec_id)
        if not s:
            _USER_RECS.pop(uid, None)
    if _USER_ACTIVE.get(uid) == rec_id:
        left = _USER_RECS.get(uid)
        if left:
            _USER_ACTIVE[uid] = next(iter(left))
        else:
            _USER_ACTIVE.pop(uid, None)


def _neon_bar(pct: float, width: int = 14) -> str:
    pct = max(0.0, min(100.0, float(pct)))
    fill = int(round(pct / 100.0 * width))
    return "▓" * fill + "░" * (width - fill)


def neon_rec_text(model: str, info: dict, rec_id: str = "") -> str:
    el = int(info.get("elapsed") or 0)
    left = info.get("left")
    bts = int(info.get("bytes") or 0)
    parts = info.get("parts") or 1
    q = info.get("quality") or "?"
    res = info.get("res") or ""
    spd = int(bts / max(el, 1))
    if left is not None and left >= 0:
        total = el + int(left)
        pct = (el / max(total, 1)) * 100
        bar = _neon_bar(pct)
        eta = f"⏳ `{fmt_dur(int(left))}`"
        pct_s = f"{pct:5.1f}%"
    else:
        pulse = int((el // 2) % 14)
        bar = "░" * pulse + "▓" + "░" * (13 - pulse)
        eta = "♾ until offline"
        pct_s = "LIVE"
    res_bit = f" · `{res}`" if res else ""
    return (
        "╭─ ⟨ <b>ＮＥＯＮ ＲＥＣ</b> ⟩ ─╮\n"
        f"│ 🔴 <b>{model}</b>\n"
        f"│ <code>{bar}</code>  <b>{pct_s}</b>\n"
        f"│ ⏱ `{fmt_dur(el)}`  {eta}\n"
        f"│ 💾 `{humanbytes(bts)}`  📶 `{humanbytes(spd)}/s`\n"
        f"│ 🎞 `{q}`{res_bit}  🧩 `{parts}`\n"
        "╰─ ⏹ stop = upload ─╯"
    )


def neon_stage(model: str, title: str, line: str = "") -> str:
    return (
        "╭─ ⟨ <b>ＮＥＯＮ</b> ⟩ ─╮\n"
        f"│ 💠 <b>{title}</b>\n"
        f"│ 🎬 `{model}`\n"
        + (f"│ {line}\n" if line else "")
        + "╰───────────────╯"
    )


def neon_mon_text(uid: int) -> str:
    sl = monitor.slots(uid)
    lines = ["╭─ ⟨ <b>ＡＵＴＯ ＭＯＮ</b> ⟩ ─╮"]
    if not sl:
        lines.append("│  empty — ➕ Add ya 📡 Live")
        lines.append(f"│  slots `0/{config.MAX_MONITORS}`")
    else:
        for i, s in enumerate(sl[:12], 1):
            name = s.get("model") or "?"
            stt = s.get("last_state") or "wait"
            led = {
                "live": "🟢", "rec": "🟢", "wait": "🔵",
                "private": "🔒",
            }.get(stt, "🔵")
            if user_recording_model(uid, name):
                led = "🟢"
            lines.append(f"│ {led} `{name}`")
        if len(sl) > 12:
            lines.append(f"│  … +{len(sl)-12} more")
    lines.append("╰─ online ⇒ auto rec ─╯")
    lines.append(f"max <b>{config.MAX_MONITORS}</b> · poll <code>{int(config.MONITOR_POLL)}s</code>")
    return "\n".join(lines)


def neon_mon_kb(uid: int):
    return panel.mon_ikb(uid)


HELP = (
    "🔴 **Stripchat Live Recorder**\n"
    "Neeche **buttons** se chalao — cmd zaroori nahi.\n\n"
    "📡 Live (Indian/Asian/…) · 📌 Monitor · 🔴 Record (unlimited)\n"
    "⏹ Stop · 📊 Status · 🔐 Admin (owner)\n\n"
    "Link paste bhi chalega. Public + group/ticket try."
)



def allowed(uid: int) -> bool:
    if config.ALLOW_ALL:
        return True
    if uid in config.ADMIN_IDS:
        return True
    if uid in config.ALLOWED_USERS:
        return True
    return False


def is_owner(uid: int) -> bool:
    return bool(config.OWNER_ID and uid == config.OWNER_ID) or uid in config.ADMIN_IDS


def deny_text():
    return (
        "🔒 **Access nahi hai.**\n"
        "Ye bot private live-recorder hai. Owner se ID whitelist karwao "
        "(`SC_ALLOWED_USERS`) ya `SC_ALLOW_ALL=true`."
    )


def _cmd(*names):
    names = [n.lower().lstrip("/") for n in names]

    def f(_flt, _client, m: Message):
        if not m or not getattr(m, "text", None) or m.media:
            return False
        t = (m.text or "").strip()
        if not t.startswith("/"):
            return False
        first = t.split()[0][1:].split("@")[0].lower()
        return first in names

    return filters.create(f)


def _card_caption(st: dict) -> str:
    u = st.get("username") or "?"
    if not st.get("online"):
        return (
            f"🔴 **{u}**\n\n"
            f"📵 Offline / nahi mili\n"
            + (f"Status: `{st.get('status')}`\n" if st.get("status") else "") +
            f"Online hone pe Refresh dabao."
        )
    badges = []
    if st.get("hd"):
        badges.append("HD")
    if st.get("vr"):
        badges.append("VR")
    stt = str(st.get("status") or "")
    if st.get("private") or stt in ("groupShow", "ticketShow", "private", "p2p", "virtualPrivate"):
        badges.append({"groupShow": "GROUP", "ticketShow": "TICKET", "private": "PRIVATE",
                       "p2p": "P2P", "virtualPrivate": "VIP"}.get(stt, "LOCKED"))
    else:
        badges.append("PUBLIC")
    if stt and stt not in badges:
        badges.append(stt)
    warn = ""
    if st.get("private") or stt in ("groupShow", "ticketShow", "private", "p2p", "virtualPrivate"):
        warn = ("⚠️ Group/ticket/private — bina logged-in cookie ke CDN 403 de sakta hai.\n"
                "Record try allowed. Cookie: `STRIPCHAT_COOKIE` / cookies.txt\n")
    else:
        warn = "Duration choose karo, phir quality:"
    return (
        f"🔴 **{u}**\n\n"
        f"{'📡 **LIVE**' if st.get('online') else '📵'}"
        f" | 👀 `{st.get('viewers', 0)}` | 🌍 `{st.get('country') or '??'}`\n"
        f"🏷 `{' | '.join(badges)}` | id `{st.get('id') or '?'}`\n\n"
        + warn
    )


def _card_kb(st: dict):
    u = st["username"]
    rows = []
    if st.get("online"):
        rows.append([
            InlineKeyboardButton("1m", callback_data=f"sc:q:{u}:60"),
            InlineKeyboardButton("2m", callback_data=f"sc:q:{u}:120"),
            InlineKeyboardButton("5m", callback_data=f"sc:q:{u}:300"),
            InlineKeyboardButton("10m", callback_data=f"sc:q:{u}:600"),
        ])
        rows.append([
            InlineKeyboardButton("15m", callback_data=f"sc:q:{u}:900"),
            InlineKeyboardButton("30m", callback_data=f"sc:q:{u}:1800"),
            InlineKeyboardButton("♾ Stop", callback_data=f"sc:q:{u}:0"),
        ])
    rows.append([
        InlineKeyboardButton("📌 Auto Monitor", callback_data=f"sc:mon:{u}"),
        InlineKeyboardButton("🔄 Refresh", callback_data=f"sc:card:{u}"),
    ])
    rows.append([
        InlineKeyboardButton("🏠 Home", callback_data="sc:m:home"),
        InlineKeyboardButton("✖️ Close", callback_data="sc:close"),
    ])
    return InlineKeyboardMarkup(rows)


_QUAL_FALLBACK = [
    ("Best", "source"), ("720p", "720p"), ("480p", "480p"),
    ("360p", "360p"), ("240p", "240p"), ("160p", "160p"),
]


def _qual_kb(model: str, dur: int, variants=None):
    rows = []
    used = set()
    btns = []
    if variants:
        for v in variants:
            name = (v.get("name") or "source").strip()
            res = v.get("res") or ""
            key = name.lower().replace(" ", "")
            if key in used:
                continue
            used.add(key)
            label = "Best" if key in ("source", "orig") else name
            if res:
                label = f"{label} {res.split('x')[0]}p" if "x" in res else f"{label} {res}"
            btns.append((label[:18], key[:16] or "source"))
    if not btns:
        btns = list(_QUAL_FALLBACK)
    else:
        # ensure extra common rungs if playlist skipped some names
        have = {k for _l, k in btns}
        for lab, key in _QUAL_FALLBACK:
            if key not in have and key != "source":
                btns.append((lab, key))
    row = []
    for lab, key in btns[:8]:
        row.append(InlineKeyboardButton(lab, callback_data=f"sc:rec:{model}:{dur}:{key}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data=f"sc:card:{model}")])
    return InlineKeyboardMarkup(rows)


async def send_card(client: Client, m: Message, st: dict):
    cap = _card_caption(st)
    kb = _card_kb(st)
    prev = st.get("preview") or ""
    if prev:
        try:
            return await m.reply_photo(prev, caption=cap, reply_markup=kb)
        except Exception:
            pass
    return await m.reply_text(cap, reply_markup=kb, disable_web_page_preview=True)


async def lookup_and_card(client, m: Message, model: str):
    wait = await m.reply_text(f"🔍 **{model}** check ho raha hai…")
    try:
        async with aiohttp.ClientSession() as session:
            st = await fetch_model_status(session, model)
    except Exception as e:
        try:
            await wait.edit_text("❌ Status fail.")
            vanish(wait, 12)
        except Exception:
            pass
        return
    try:
        await wait.delete()
    except Exception:
        pass
    await send_card(client, m, st)


# ---------- commands ----------
async def cmd_start(client: Client, m: Message):
    uid = m.from_user.id if m.from_user else 0
    if not allowed(uid):
        return await m.reply_text(deny_text())
    await m.reply_text(
        panel.home_text(),
        reply_markup=panel.reply_kb(uid),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
    await m.reply_text(
        "🎛️ <b>Panel</b> — tap a button",
        reply_markup=panel.home_ikb(uid),
        parse_mode=ParseMode.HTML,
    )


async def cmd_rec(client: Client, m: Message):
    uid = m.from_user.id if m.from_user else 0
    if not allowed(uid):
        return await m.reply_text(deny_text())
    parts = (m.text or "").split(None, 1)
    arg = parts[1] if len(parts) > 1 else ""
    if not arg and m.reply_to_message and m.reply_to_message.text:
        arg = m.reply_to_message.text
    model = model_from_input(arg)
    if not model:
        return vanish(await m.reply_text("Usage: `/rec ModelName` ya link paste karo."), 12)
    await lookup_and_card(client, m, model)


async def cmd_live(client: Client, m: Message):
    uid = m.from_user.id if m.from_user else 0
    if not allowed(uid):
        return await m.reply_text(deny_text())
    parts = (m.text or "").split(None, 1)
    tag = parts[1].strip().lower() if len(parts) > 1 else "girls"
    if tag not in TAGS:
        tag = "girls"
    wait = await m.reply_text(f"🔍 Online **{tag}**…")
    async with aiohttp.ClientSession() as session:
        models, total = await fetch_online_models(session, tag=tag, limit=8, offset=0)
    try:
        await wait.delete()
    except Exception:
        pass
    if not models:
        return await m.reply_text(f"❌ {tag} list nahi mili (API block ho sakti hai). Link paste karke try karo.")
    await m.reply_text(
        _browse_text(models, tag, 0, total),
        reply_markup=_browse_kb(models, tag, 0, total),
        disable_web_page_preview=True,
    )


async def cmd_stop(client: Client, m: Message):
    uid = m.from_user.id if m.from_user else 0
    parts = (m.text or "").split(None, 1)
    want = model_from_input(parts[1]) if len(parts) > 1 else ""
    ids = list(_USER_RECS.get(uid) or [])
    if not ids:
        rid = _USER_ACTIVE.get(uid)
        if rid:
            ids = [rid]
    stopped = 0
    for rec_id in ids:
        rec = _RECS.get(rec_id)
        if not rec:
            continue
        if want and (rec.get("model") or "").lower() != want.lower():
            continue
        rec["stop"].set()
        stopped += 1
    if not stopped:
        return vanish(await m.reply_text("Koi recording nahi chal rahi."), 12)
    await m.reply_text(f"🛑 Stop ×{stopped} — neon upload start hoga…")


async def cmd_stat(client: Client, m: Message):
    uid = m.from_user.id if m.from_user else 0
    ids = [r for r in (_USER_RECS.get(uid) or []) if r in _RECS]
    if not ids:
        rid = _USER_ACTIVE.get(uid)
        if rid and rid in _RECS:
            ids = [rid]
    await m.reply_text(
        dash_text(uid) + "\n\n" + neon_mon_text(uid),
        reply_markup=neon_mon_kb(uid), parse_mode=ParseMode.HTML,
    )



async def cmd_mon(client: Client, m: Message):
    uid = m.from_user.id if m.from_user else 0
    if not allowed(uid):
        return await m.reply_text(deny_text())
    parts = (m.text or "").split()
    arg = " ".join(parts[1:]) if len(parts) > 1 else ""
    if not arg:
        return await m.reply_text(neon_mon_text(uid), reply_markup=neon_mon_kb(uid))
    bits = arg.split()
    model = model_from_input(bits[0])
    quality = "source"
    if len(bits) > 1:
        q = bits[1].lower().replace(" ", "")
        if q in ("source", "best", "720p", "720", "480p", "480", "360p", "240p", "160p"):
            quality = {"best": "source", "720": "720p", "480": "480p"}.get(q, q)
    if not model:
        return await m.reply_text("Usage: `/mon ModelName`  (optional quality: `/mon Model 720p`)")
    ok, msg = monitor.add(uid, model, quality)
    await m.reply_text(
        (msg + "\n\n" if ok else "⚠️ " + msg + "\n\n") + neon_mon_text(uid),
        reply_markup=neon_mon_kb(uid),
        parse_mode=ParseMode.HTML,
    )


async def cmd_unmon(client: Client, m: Message):
    uid = m.from_user.id if m.from_user else 0
    if not allowed(uid):
        return await m.reply_text(deny_text())
    parts = (m.text or "").split(None, 1)
    arg = (parts[1] if len(parts) > 1 else "").strip()
    if not arg:
        return await m.reply_text("Usage: `/unmon Model` ya `/unmon all`")
    if arg.lower() in ("all", "*", "clear"):
        n = monitor.clear(uid)
        return await m.reply_text(f"🗑 {n} slot(s) clear.\n\n" + neon_mon_text(uid),
                                  reply_markup=neon_mon_kb(uid))
    model = model_from_input(arg) or arg
    if monitor.remove(uid, model):
        await m.reply_text(f"🗑 `{model}` hata diya.\n\n" + neon_mon_text(uid),
                           reply_markup=neon_mon_kb(uid))
    else:
        await m.reply_text(f"`{model}` monitor pe nahi thi.")


async def cmd_admin(client: Client, m: Message):
    uid = m.from_user.id if m.from_user else 0
    if not allowed(uid):
        return await m.reply_text(deny_text())
    first = ((m.text or "").split() or [""])[0].lstrip("/").split("@")[0].lower()
    if first == "menu":
        return await cmd_start(client, m)
    if not is_owner(uid):
        return await cmd_start(client, m)
    await m.reply_text(
        panel.admin_text(), reply_markup=panel.admin_ikb(), parse_mode=ParseMode.HTML,
    )


async def cmd_keys(client: Client, m: Message):
    uid = m.from_user.id if m.from_user else 0
    if not is_owner(uid):
        return await m.reply_text("Owner only.")
    km = load_key_map()
    lines = [f"`{k[:6]}…` → pdkey {'✅' if v else '❌'}" for k, v in list(km.items())[:20]]
    await m.reply_text(
        f"🔑 **{len(km)}** pair(s) from `{config.KEY_FILE}`\n" +
        ("\n".join(lines) if lines else "empty — keys.txt me `pkey:pdkey` daalo")
    )


URL_RE = re.compile(r"https?://[^\s<>]+", re.I)


async def handle_menu_action(client: Client, m: Message, action: str):
    uid = m.from_user.id if m.from_user else 0
    if action == "home":
        return await cmd_start(client, m)
    if action == "live":
        wait = await m.reply_text("🔍 Online **girls**…")
        async with aiohttp.ClientSession() as session:
            models, total = await fetch_online_models(session, tag="girls", limit=8, offset=0)
        try:
            await wait.delete()
        except Exception:
            pass
        if not models:
            return vanish(await m.reply_text("❌ live list nahi mili. Link paste karo."), 12)
        return await m.reply_text(
            _browse_text(models, "girls", 0, total),
            reply_markup=_browse_kb(models, "girls", 0, total),
            disable_web_page_preview=True,
        )
    if action == "mon":
        return await m.reply_text(
            neon_mon_text(uid), reply_markup=neon_mon_kb(uid), parse_mode=ParseMode.HTML,
        )
    if action == "rec":
        panel.set_wait(uid, "rec")
        return await m.reply_text(
            "🔴 <b>Record</b> — model naam ya Stripchat link bhejo.\n⏱ 3 min",
            parse_mode=ParseMode.HTML,
        )
    if action == "stop":
        n = 0
        for rec_id in list(_USER_RECS.get(uid) or []):
            rec = _RECS.get(rec_id)
            if rec:
                rec["stop"].set()
                n += 1
        if not n:
            return vanish(await m.reply_text("Koi recording nahi chal rahi."), 12)
        return await m.reply_text(f"🛑 Stop ×{n} — upload start hoga…")
    if action == "stat":
        return await cmd_stat(client, m)
    if action == "admin":
        if not is_owner(uid):
            return await m.reply_text("Owner only.")
        return await m.reply_text(
            panel.admin_text(), reply_markup=panel.admin_ikb(), parse_mode=ParseMode.HTML,
        )


async def on_paste(client: Client, m: Message):
    uid = m.from_user.id if m.from_user else 0
    if not allowed(uid):
        return await m.reply_text(deny_text())
    text = (m.text or "").strip()
    if text in panel.KB_MAP:
        return await handle_menu_action(client, m, panel.KB_MAP[text])
    w = panel.pop_wait(uid)
    if w:
        act = w.get("a")
        model = model_from_input(text)
        if not model and act in ("mon", "rec"):
            model = model_from_input(text.split()[0]) if text.split() else ""
        if act in ("mon", "amon") and model:
            ok, msg = monitor.add(uid, model, "source")
            return await m.reply_text(
                (msg + "\n\n" if ok else "⚠️ " + msg + "\n\n") + neon_mon_text(uid),
                reply_markup=neon_mon_kb(uid), parse_mode=ParseMode.HTML,
            )
        if act == "rec" and model:
            return await lookup_and_card(client, m, model)
        return vanish(await m.reply_text("Naam samajh nahi aaya. Button se phir try karo."), 12)
    text = m.text or ""
    urls = URL_RE.findall(text)
    found = []
    for u in urls:
        if is_stripchat_url(u):
            mm = model_from_input(u)
            if mm and mm.lower() not in {x.lower() for x in found}:
                found.append(mm)
    if len(found) > 1:
        n_ok = 0
        for mm in found:
            ok, _msg = await begin_recording(client, uid, mm, 0, "source")
            if ok:
                n_ok += 1
        return await m.reply_text(
            dash_text(uid) + f"\n\n📎 {n_ok}/{len(found)} until-stop rec",
            parse_mode=ParseMode.HTML,
        )
    model = found[0] if found else ""
    if not model:
        model = model_from_input(text.strip())
        if not model:
            return
        if " " in text.strip():
            return
    await lookup_and_card(client, m, model)


def _browse_text(models, tag, offset, total):
    txt = f"🔴 **LIVE — {tag.upper()}** (total {total})\n\n"
    for i, mdl in enumerate(models, offset + 1):
        flags = (" HD" if mdl.get("isHd") else "") + (" VR" if mdl.get("isVr") else "")
        txt += (f"**{i}.** `{mdl.get('username')}` — 👀 {mdl.get('viewersCount', 0)}"
                f" | 🌍 {str(mdl.get('country') or '??').upper()}{flags}\n")
    txt += "\nTap karke card kholo."
    return txt


def _browse_kb(models, tag, offset, total, step=8):
    rows = []
    for mdl in models:
        un = mdl.get("username") or ""
        rows.append([
            InlineKeyboardButton(
                f"🔴 {un} ({mdl.get('viewersCount', 0)})",
                callback_data=f"sc:card:{un}",
            ),
            InlineKeyboardButton("📌", callback_data=f"sc:mon:{un}"),
        ])
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"sc:pg:{tag}:{max(0, offset-step)}"))
    if offset + step < total and models:
        nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"sc:pg:{tag}:{offset+step}"))
    if nav:
        rows.append(nav)
    row = []
    for tname in TAGS:
        lab = tname.upper()[:7]
        if tname == tag:
            lab = "✅" + lab
        row.append(InlineKeyboardButton(lab, callback_data=f"sc:pg:{tname}:0"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([
        InlineKeyboardButton("🔴 REC PAGE (all live)", callback_data=f"sc:mall:{tag}:{offset}"),
    ])
    rows.append([
        InlineKeyboardButton("🏠 Home", callback_data="sc:m:home"),
        InlineKeyboardButton("✖️ Close", callback_data="sc:close"),
    ])
    return InlineKeyboardMarkup(rows)


# ---------- record task ----------
async def _upload_one(client, status_msg, uid, path, model, idx, total, quiet=False):
    w, h, dur = probe_video(path)
    thumb = os.path.splitext(path)[0] + "_th.jpg"
    tpath = await make_thumb(path, thumb)
    title = f"{model} LIVE {time.strftime('%d-%b %H:%M')}"
    started = time.time()
    last = [0.0]

    async def _prog(cur, tot):
        if quiet:
            return
        now = time.time()
        if now - last[0] < 3:
            return
        last[0] = now
        el = max(now - started, 0.001)
        try:
            await safe_edit(
                status_msg,
                neon_stage(
                    title.split()[0] if title else "upload",
                    "UPLOAD",
                    f"`{humanbytes(cur)}/{humanbytes(tot)}`  📶 `{humanbytes(int(cur / el))}/s`"
                    + (f"  part `{idx}/{total}`" if total > 1 else ""),
                ),
                flood_sleep=False,
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    cap = f"🎥 **{title}**\n🔴 Stripchat LIVE"
    if total > 1:
        cap += f" | Part {idx}/{total}"
    for _try in range(5):
        try:
            await client.send_video(
                chat_id=uid, video=path, caption=cap,
                duration=max(dur, 1), width=w or 0, height=h or 0,
                supports_streaming=True,
                thumb=tpath if tpath else None,
                progress=_prog,
            )
            break
        except FloodWait as e:
            wsec = min(int(getattr(e, "value", 8) or 8), 60)
            logger.warning("FloodWait upload %ss", wsec)
            await asyncio.sleep(wsec + 1)
        except Exception:
            if _try == 4:
                raise
            await asyncio.sleep(2)
    if config.LOG_CHANNEL:
        try:
            await client.send_video(
                chat_id=config.LOG_CHANNEL, video=path, caption=f"{cap}\n👤 `{uid}`",
                duration=max(dur, 1), width=w or 0, height=h or 0,
                supports_streaming=True, thumb=tpath if tpath else None,
            )
        except Exception:
            pass
    if tpath:
        try:
            os.remove(tpath)
        except Exception:
            pass


async def _record_task(client, rec_id, uid, user, model, dur_seconds, quality, status_msg):
    global _UPLOADING
    rec_meta = _RECS.get(rec_id) or {}
    stop = rec_meta.get("stop") or asyncio.Event()
    work = os.path.join(config.DOWNLOAD_DIR, str(uid), rec_id)
    os.makedirs(work, exist_ok=True)
    reason = "error"
    uploaded_n = [0]
    parts = []
    part_tasks = []

    async def _do_part(path):
        global _UPLOADING
        async with _REMUX_LOCK:
            out = await remux_to_mp4(path)
        uploaded_n[0] += 1
        async with _UPLOAD_LOCK:
            _UPLOADING = model
            try:
                await _upload_one(
                    client, status_msg, uid, out, model, uploaded_n[0], 0, quiet=True,
                )
            finally:
                _UPLOADING = None
        for fp in (out, path):
            try:
                if fp and os.path.exists(fp):
                    os.remove(fp)
            except Exception:
                pass
        try:
            import gc
            gc.collect()
        except Exception:
            pass

    async def on_part(path):
        # rec loop block nahi — upload background
        part_tasks.append(asyncio.create_task(_do_part(path)))

    async def _wait_parts():
        if part_tasks:
            await asyncio.gather(*part_tasks, return_exceptions=True)
            part_tasks.clear()

    try:
        conn = aiohttp.TCPConnector(
            limit=8, ttl_dns_cache=300, enable_cleanup_closed=True, ssl=False,
        )
        async with aiohttp.ClientSession(connector=conn) as session:
            model_id = int(rec_meta.get("model_id") or 0)
            if not model_id:
                st = await fetch_model_status(session, model)
                if not st.get("id"):
                    raise MouflonError("OFFLINE")
                if not st.get("online"):
                    raise MouflonError("OFFLINE")
                model_id = int(st["id"])

            async def on_tick(info):
                # FloodWait pe sleep NAHI — warna HLS rec ruk jati
                await safe_edit(
                    status_msg,
                    neon_rec_text(model, info, rec_id),
                    flood_sleep=False,
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("⏹ STOP & UPLOAD", callback_data=f"sc:stop:{rec_id}")]]
                    ),
                )

            parts, reason, total_bytes, chosen = await record_to_parts(
                session, model, model_id, quality, dur_seconds, stop, work,
                until_stop_cap=config.UNTIL_STOP_CAP_MIN * 60,
                on_tick=on_tick,
                on_part=on_part,
            )
    except MouflonError as e:
        es = str(e)
        stt = "private" if es == "PRIVATE" or "403" in es else "wait"
        if rec_meta.get("monitor"):
            try:
                await status_msg.delete()
            except Exception:
                pass
            await _wait_parts()
            cleanup_dir(work)
            _RECS.pop(rec_id, None)
            _track_del(uid, rec_id)
            monitor.touch_end(uid, model, stt)
            try:
                asyncio.create_task(kick_queue(client))
            except Exception:
                pass
            return
        try:
            short = "🔒 Group/private — public aate hi naya part."
            if es in ("OFFLINE",) or "404" in es:
                short = "📴 Public live band."
            await status_msg.edit_text(short)
            vanish(status_msg, 12)
        except Exception:
            pass
        cleanup_dir(work)
        _RECS.pop(rec_id, None)
        _track_del(uid, rec_id)
        try:
            asyncio.create_task(kick_queue(client))
        except Exception:
            pass
        return
    except Exception as e:
        logger.exception("record crash")
        if rec_meta.get("monitor"):
            try:
                await status_msg.delete()
            except Exception:
                pass
        else:
            try:
                await status_msg.edit_text("⚠️ Rec error — retry.")
                vanish(status_msg, 12)
            except Exception:
                pass
        cleanup_dir(work)
        _RECS.pop(rec_id, None)
        _track_del(uid, rec_id)
        if rec_meta.get("monitor"):
            monitor.touch_end(uid, model, "wait")
        try:
            asyncio.create_task(kick_queue(client))
        except Exception:
            pass
        return

    await _wait_parts()
    label = {
        "stopped": "🛑 stopped", "offline": "📴 public ended",
        "private": "🔒 group/private — next public = naya video",
        "duration": "⏱ done",
    }.get(reason, reason)
    if not parts and not uploaded_n[0]:
        if rec_meta.get("monitor"):
            try:
                await status_msg.delete()
            except Exception:
                pass
            cleanup_dir(work)
            _RECS.pop(rec_id, None)
            _track_del(uid, rec_id)
            monitor.touch_end(uid, model, "private" if reason == "private" else "wait")
            try:
                asyncio.create_task(kick_queue(client))
            except Exception:
                pass
            return
        try:
            await status_msg.edit_text(f"⚠️ **{model}** — capture nahi ({label}).")
            vanish(status_msg, 12)
        except Exception:
            pass
        cleanup_dir(work)
        _RECS.pop(rec_id, None)
        _track_del(uid, rec_id)
        try:
            asyncio.create_task(kick_queue(client))
        except Exception:
            pass
        return

    if not parts and uploaded_n[0]:
        try:
            await status_msg.delete()
        except Exception:
            pass
        try:
            done = await client.send_message(
                uid, f"✅ **{model}** — {uploaded_n[0]} uploaded ({label}).",
            )
            vanish(done, 12)
        except Exception:
            pass
        cleanup_dir(work)
        _RECS.pop(rec_id, None)
        _track_del(uid, rec_id)
        if rec_meta.get("monitor"):
            monitor.touch_end(uid, model, "wait" if reason != "private" else "private")
        try:
            asyncio.create_task(kick_queue(client))
        except Exception:
            pass
        return

    finals = []
    for i, pth in enumerate(parts, 1):
        try:
            await safe_edit(status_msg, neon_stage(model, "REMUX", f"part `{i}/{len(parts)}`"), parse_mode=ParseMode.HTML)
        except Exception:
            pass
        async with _REMUX_LOCK:
            finals.append(await remux_to_mp4(pth))

    ok = uploaded_n[0]
    total_u = uploaded_n[0] + len(finals)
    for i, path in enumerate(finals, 1):
        try:
            await safe_edit(status_msg, neon_stage(model, "UPLOAD", f"part `{uploaded_n[0]+i}/{total_u}`"), parse_mode=ParseMode.HTML)
            async with _UPLOAD_LOCK:
                _UPLOADING = model
                try:
                    await _upload_one(client, status_msg, uid, path, model, uploaded_n[0]+i, total_u)
                finally:
                    _UPLOADING = None
            ok += 1
        except Exception as e:
            logger.exception("upload fail")
            try:
                await status_msg.reply_text(f"❌ Part {i} upload fail: `{str(e)[:200]}`")
            except Exception:
                pass
    try:
        await status_msg.delete()
    except Exception:
        pass
    try:
        done = await client.send_message(
            uid,
            f"✅ **{model}** — {ok} uploaded ({label})."
            if ok else f"❌ **{model}** upload fail ({label}).",
        )
        vanish(done, 12)
    except Exception:
        pass
    cleanup_dir(work)
    _RECS.pop(rec_id, None)
    _track_del(uid, rec_id)
    if rec_meta.get("monitor"):
        monitor.touch_end(uid, model, "wait" if reason != "private" else "private")
    try:
        asyncio.create_task(kick_queue(client))
    except Exception:
        pass


def rec_capped(uid=None) -> bool:
    g = int(getattr(config, "MAX_CONCURRENT_REC", 0) or 0)
    if g and len(_RECS) >= g:
        return True
    if uid is not None:
        u = int(getattr(config, "MAX_REC_PER_USER", 0) or 0)
        if u and user_rec_count(uid) >= u:
            return True
    return False


def _q_has(uid, model):
    m = (model or "").lower()
    return any(x.get("uid") == uid and (x.get("model") or "").lower() == m for x in _Q)


def enqueue_rec(uid, model, dur, quality, from_monitor=False, model_id=0):
    if _q_has(uid, model) or user_recording_model(uid, model):
        return False, "already queued/rec"
    _Q.append({
        "uid": uid, "model": model, "dur": dur, "quality": quality or "source",
        "monitor": bool(from_monitor), "model_id": int(model_id or 0),
    })
    return True, f"queued `{model}` (#{len(_Q)})"


async def kick_queue(client):
    """Free slot → queue se next rec (only if SC_MAX_REC set)."""
    try:
        async with _START_LOCK:
            if not _Q:
                return
            if rec_capped():
                return
            nxt = None
            for i, item in enumerate(list(_Q)):
                uid = item.get("uid")
                if rec_capped(uid):
                    continue
                if user_recording_model(uid, item.get("model") or ""):
                    _Q.pop(i)
                    continue
                nxt = _Q.pop(i)
                break
        if not nxt:
            return
        await begin_recording(
            client, nxt["uid"], nxt["model"], nxt.get("dur") or 0,
            nxt.get("quality") or "source",
            from_monitor=bool(nxt.get("monitor")),
            model_id=int(nxt.get("model_id") or 0),
        )
    except Exception:
        logger.exception("kick_queue")


async def begin_recording(client, uid: int, model: str, dur: int, quality: str,
                          from_monitor: bool = False, reply_msg=None, model_id: int = 0):
    """Start a rec. Returns (ok: bool, err: str). Full = queue, crash nahi."""
    async with _START_LOCK:
        if not allowed(uid):
            return False, "Access nahi."
        if user_recording_model(uid, model):
            return False, f"`{model}` pehle se record ho rahi hai."
        if rec_capped(uid):
            ok, msg = enqueue_rec(uid, model, dur, quality, from_monitor, model_id)
            return ok, msg
        rec_id = f"r{uid}_{secrets.token_hex(3)}"
        _RECS[rec_id] = {
            "stop": asyncio.Event(), "user_id": uid, "model": model,
            "t0": time.time(), "monitor": bool(from_monitor),
            "model_id": int(model_id or 0),
        }
        _track_add(uid, rec_id)
    status_msg = await safe_send(
        client, uid, neon_stage(model, "BOOT", f"`{quality}` · starting…"),
        flood_sleep=True,
        parse_mode=ParseMode.HTML,
    )
    if status_msg is None:
        try:
            status_msg = await client.send_message(
                uid, f"🔴 REC `{model}` starting…",
            )
        except Exception as e:
            _RECS.pop(rec_id, None)
            _track_del(uid, rec_id)
            return False, f"telegram send fail: {e}"
    asyncio.create_task(_record_task(client, rec_id, uid, None, model, dur, quality, status_msg))
    return True, rec_id


async def start_recording(client, c: CallbackQuery, model: str, dur: int, quality: str):
    uid = c.from_user.id
    ok, err = await begin_recording(client, uid, model, dur, quality, from_monitor=False)
    if not ok:
        return await c.answer(str(err)[:180], show_alert=True)
    if str(err).startswith("queued"):
        return await c.answer("⏳ Queue me — next slot pe start", show_alert=True)
    await c.answer("🔴 Recording start!")


async def on_cb(client: Client, c: CallbackQuery):
    data = c.data or ""
    uid = c.from_user.id if c.from_user else 0
    try:
        if data == "sc:close":
            try:
                await c.message.delete()
            except Exception:
                pass
            return await c.answer()
        if data.startswith("sc:stop:"):
            rec_id = data.split(":", 2)[2]
            rec = _RECS.get(rec_id)
            if rec and (uid == rec.get("user_id") or is_owner(uid)):
                rec["stop"].set()
                return await c.answer("🛑 Stop — upload hoga")
            return await c.answer("Recording active nahi.", show_alert=True)
        if not allowed(uid):
            return await c.answer("Access nahi.", show_alert=True)
        if data == "sc:m:home":
            await c.answer()
            await panel.edit_html(c, panel.home_text(), panel.home_ikb(uid))
            return
        if data == "sc:m:mon" or data.startswith("sc:m:mon:"):
            page = 0
            if data.startswith("sc:m:mon:"):
                try:
                    page = int(data.split(":")[3])
                except Exception:
                    page = 0
            await c.answer()
            await panel.edit_html(c, neon_mon_text(uid), panel.mon_ikb(uid, page))
            return
        if data == "sc:m:stat":
            await c.answer()
            ids = [r for r in (_USER_RECS.get(uid) or []) if r in _RECS]
            lines = ["╭─ ⟨ <b>ＳＴＡＴ</b> ⟩ ─╮"]
            if not ids:
                lines.append("│  rec idle")
            for rec_id in ids:
                rec = _RECS[rec_id]
                el = int(time.time() - rec.get("t0", time.time()))
                tag = "📌" if rec.get("monitor") else "🎙"
                lines.append(f"│ {tag} <b>{rec.get('model')}</b>  ⏱ {fmt_dur(el)}")
            lines.append("╰───────────────╯")
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("⏹ Stop all", callback_data="sc:m:stop"),
                 InlineKeyboardButton("🔄", callback_data="sc:m:stat")],
                [InlineKeyboardButton("⬅️ Home", callback_data="sc:m:home")],
            ])
            await panel.edit_html(c, dash_text(uid) + "\n\n" + neon_mon_text(uid), kb)
            return
        if data == "sc:m:stop":
            n = 0
            for rec_id in list(_USER_RECS.get(uid) or []):
                rec = _RECS.get(rec_id)
                if rec:
                    rec["stop"].set()
                    n += 1
            await c.answer(f"stop ×{n}" if n else "koi rec nahi", show_alert=True)
            return
        if data == "sc:m:rec":
            panel.set_wait(uid, "rec")
            await c.answer()
            await panel.edit_html(
                c,
                "🔴 <b>Record</b> — abhi model naam ya link bhejo.\n⏱ 3 min",
                InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Home", callback_data="sc:m:home")]]),
            )
            return
        if data == "sc:m:askmon":
            panel.set_wait(uid, "mon")
            await c.answer()
            await panel.edit_html(
                c,
                "📌 <b>Monitor add</b> — model naam ya link bhejo.\n"
                f"slots {len(monitor.slots(uid))}/{config.MAX_MONITORS} · ⏱ 3 min",
                InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Monitors", callback_data="sc:m:mon")]]),
            )
            return
        if data == "sc:m:live":
            await c.answer("🔍 live…")
            async with aiohttp.ClientSession() as session:
                models, total = await fetch_online_models(session, tag="girls", limit=8, offset=0)
            if not models:
                return await c.answer("Live list nahi mili — link paste karo.", show_alert=True)
            try:
                await c.message.edit_text(
                    _browse_text(models, "girls", 0, total),
                    reply_markup=_browse_kb(models, "girls", 0, total),
                    disable_web_page_preview=True,
                )
            except Exception:
                try:
                    await c.message.reply_text(
                        _browse_text(models, "girls", 0, total),
                        reply_markup=_browse_kb(models, "girls", 0, total),
                        disable_web_page_preview=True,
                    )
                except Exception:
                    pass
            return
        if data.startswith("sc:ad:"):
            if not is_owner(uid):
                return await c.answer("Owner only.", show_alert=True)
            sub = data.split(":", 2)[2]
            if sub == "home":
                await c.answer()
                await panel.edit_html(c, panel.admin_text(), panel.admin_ikb())
                return
            if sub == "recs":
                await c.answer()
                txt, kb = panel.recs_text_kb()
                await panel.edit_html(c, txt, kb)
                return
            if sub == "mons":
                await c.answer()
                txt, kb = panel.mons_text_kb()
                await panel.edit_html(c, txt, kb)
                return
            if sub == "users":
                await c.answer()
                await panel.edit_html(
                    c, panel.users_text(),
                    InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Admin", callback_data="sc:ad:home")]]),
                )
                return
            if sub == "stopall":
                n = 0
                for rec in list(_RECS.values()):
                    rec["stop"].set()
                    n += 1
                await c.answer(f"stopped {n}", show_alert=True)
                await panel.edit_html(c, panel.admin_text(), panel.admin_ikb())
                return
            if sub == "gc":
                msg = panel.ram_clean()
                await c.answer(msg[:180], show_alert=True)
                await panel.edit_html(c, panel.admin_text() + "\n" + msg, panel.admin_ikb())
                return
            if sub == "keys":
                km = load_key_map()
                lines = [f"<code>{k[:8]}…</code> {'✅' if v else '❌'}" for k, v in list(km.items())[:16]]
                txt = f"🔑 <b>{len(km)}</b> key pair(s)\n" + ("\n".join(lines) or "empty")
                await c.answer()
                await panel.edit_html(
                    c, txt,
                    InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Admin", callback_data="sc:ad:home")]]),
                )
                return
            if sub == "cookie":
                ck = load_sc_cookie()
                txt = "🍪 Cookie <b>set</b> — group/ticket try OK." if ck else "🍪 Cookie <b>nahi</b> — public HLS only."
                await c.answer()
                await panel.edit_html(
                    c, txt,
                    InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Admin", callback_data="sc:ad:home")]]),
                )
                return
            if sub.startswith("um:"):
                rest = sub[3:]
                uid_s, _, model = rest.partition(":")
                try:
                    tu = int(uid_s)
                except Exception:
                    return await c.answer("bad uid")
                monitor.remove(tu, model)
                await c.answer(f"unmon {model}")
                txt, kb = panel.mons_text_kb()
                await panel.edit_html(c, txt, kb)
                return
            await c.answer()
            return
        if data.startswith("sc:q:"):
            # sc:q:model:dur
            _, _, rest = data.partition("sc:q:")
            model, _, dur_s = rest.rpartition(":")
            await c.answer()
            try:
                await c.message.edit_text(
                    f"🎞 **{model}** — quality choose karo\n⏱ `{fmt_dur(int(dur_s)) if int(dur_s) else 'until stop'}`",
                    reply_markup=_qual_kb(model, int(dur_s)),
                )
            except Exception:
                try:
                    await c.message.edit_caption(
                        f"🎞 **{model}** — quality choose karo",
                        reply_markup=_qual_kb(model, int(dur_s)),
                    )
                except Exception:
                    pass
            return
        if data.startswith("sc:rec:"):
            # sc:rec:model:dur:quality
            parts = data.split(":")
            # sc rec model dur quality — model may contain no colons
            if len(parts) < 5:
                return await c.answer("bad rec data", show_alert=True)
            quality = parts[-1]
            dur = int(parts[-2])
            model = ":".join(parts[2:-2])
            return await start_recording(client, c, model, dur, quality)
        if data.startswith("sc:mon:"):
            model = data.split(":", 2)[2]
            ok, msg = monitor.add(uid, model, "source")
            await c.answer(msg[:180], show_alert=True)
            try:
                await c.message.reply_text(neon_mon_text(uid), reply_markup=neon_mon_kb(uid), parse_mode=ParseMode.HTML)
            except Exception:
                pass
            return
        if data.startswith("sc:unmon:"):
            model = data.split(":", 2)[2]
            if model.lower() == "all":
                monitor.clear(uid)
                await c.answer("all clear")
            else:
                monitor.remove(uid, model)
                await c.answer(f"unmon {model}")
            try:
                await c.message.edit_text(neon_mon_text(uid), reply_markup=neon_mon_kb(uid), parse_mode=ParseMode.HTML)
            except Exception:
                pass
            return
        if data.startswith("sc:card:"):
            model = data.split(":", 2)[2]
            await c.answer("🔄 …")
            async with aiohttp.ClientSession() as session:
                st = await fetch_model_status(session, model)
            cap, kb = _card_caption(st), _card_kb(st)
            try:
                await c.message.edit_caption(cap, reply_markup=kb)
            except Exception:
                try:
                    await c.message.edit_text(cap, reply_markup=kb, disable_web_page_preview=True)
                except Exception:
                    pass
            return
        if data.startswith("sc:mall:"):
            # sc:mall:tag:offset — is page ke models monitor queue me
            parts = data.split(":")
            tag = parts[2] if len(parts) > 2 else "girls"
            offset = int(parts[3]) if len(parts) > 3 else 0
            await c.answer("📌 adding…")
            async with aiohttp.ClientSession() as session:
                models, total = await fetch_online_models(session, tag=tag, limit=8, offset=offset)
            added = 0
            started = 0
            for mdl in models:
                un = mdl.get("username") or ""
                if not un:
                    continue
                ok, _msg = monitor.add(uid, un, "source")
                if ok:
                    added += 1
                ok2, _e = await begin_recording(
                    client, uid, un, 0, "source", from_monitor=True,
                )
                if ok2:
                    started += 1
            await c.answer(
                f"📌 {added} mon · 🔴 {started} rec start",
                show_alert=True,
            )
            try:
                await c.message.reply_text(
                    neon_mon_text(uid), reply_markup=neon_mon_kb(uid), parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
            return
        if data.startswith("sc:pg:"):
            _, _, tag, off = data.split(":", 3)
            tag = tag if tag in TAGS else "girls"
            offset = max(0, int(off))
            await c.answer()
            async with aiohttp.ClientSession() as session:
                models, total = await fetch_online_models(session, tag=tag, limit=8, offset=offset)
            if not models:
                return
            try:
                await c.message.edit_text(
                    _browse_text(models, tag, offset, total),
                    reply_markup=_browse_kb(models, tag, offset, total),
                    disable_web_page_preview=True,
                )
            except Exception:
                pass
            return
        await c.answer()
    except Exception as e:
        logger.debug("cb error: %s", e)
        try:
            await c.answer(str(e)[:100], show_alert=True)
        except Exception:
            pass


async def cmd_ping(client: Client, m: Message):
    vanish(await m.reply_text("pong ✅ bot alive"), 12)


_KNOWN_CMDS = {
    "start", "help", "ping", "rec", "str", "record", "cam",
    "live", "top", "strtop", "browse", "stop", "mystat", "status", "recstatus", "keys",
    "mon", "watch", "monitor", "unmon", "unwatch", "mons", "monitors",
    "admin", "panel", "menu",
}


async def on_unknown(client: Client, m: Message):
    uid = m.from_user.id if m.from_user else 0
    text = (m.text or "").strip()
    logger.info("in uid=%s text=%s", uid, text[:120])
    if not text.startswith("/"):
        return
    first = text.split()[0][1:].split("@")[0].lower()
    if first in _KNOWN_CMDS:
        return
    if not allowed(uid):
        return await m.reply_text(deny_text())
    return vanish(await m.reply_text("Unknown command.\n\n" + HELP, disable_web_page_preview=True), 12)


def register(app: Client):
    """Instance pe handlers lagao (class @Client decorator Koyeb pe silent tha)."""
    priv = filters.private
    app.add_handler(MessageHandler(cmd_start, priv & _cmd("start", "help")))
    app.add_handler(MessageHandler(cmd_ping, priv & _cmd("ping")))
    app.add_handler(MessageHandler(cmd_rec, priv & _cmd("rec", "str", "record", "cam")))
    app.add_handler(MessageHandler(cmd_live, priv & _cmd("live", "top", "strtop", "browse")))
    app.add_handler(MessageHandler(cmd_stop, priv & _cmd("stop")))
    app.add_handler(MessageHandler(cmd_stat, priv & _cmd("mystat", "status", "recstatus")))
    app.add_handler(MessageHandler(cmd_mon, priv & _cmd("mon", "watch", "monitor", "mons", "monitors")))
    app.add_handler(MessageHandler(cmd_unmon, priv & _cmd("unmon", "unwatch")))
    app.add_handler(MessageHandler(cmd_keys, priv & _cmd("keys")))
    app.add_handler(MessageHandler(cmd_admin, priv & _cmd("admin", "panel", "menu")))
    app.add_handler(MessageHandler(on_paste, priv & filters.text & ~filters.regex(r"^/")))
    app.add_handler(CallbackQueryHandler(on_cb, filters.regex(r"^sc:")))
    # last: unknown slash commands
    app.add_handler(MessageHandler(on_unknown, priv & filters.text), group=8)
    logger.info("handlers registered on client")
