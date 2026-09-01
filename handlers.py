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
from pyrogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
)

import config
from engine import (
    TAGS, MouflonError, model_from_input, is_stripchat_url,
    fetch_model_status, fetch_online_models, load_key_map,
    record_to_parts, remux_to_mp4, probe_video, make_thumb,
    cleanup_dir, humanbytes, fmt_dur,
)

logger = logging.getLogger("sc.handlers")

_RECS = {}
_USER_ACTIVE = {}

HELP = (
    "🔴 **Stripchat Live Recorder**\n"
    "Sirf Stripchat + white-label live cams.\n\n"
    "**Kaise use karein**\n"
    "• Link paste karo\n"
    "  `https://stripchat.com/Model`\n"
    "  `https://superchatlive.com/Model`\n"
    "  `https://xhamsterlive.com/Model`\n"
    "• `/rec ModelName`\n"
    "• `/live` ya `/top` — online browse\n"
    "• `/stop` — apni recording band\n"
    "• `/mystat` — running rec\n\n"
    "Public LIVE pe record hota hai. Private / group-show = 403, skip.\n"
    "Duration: 1 / 5 / 10 / 30 min ya Until Stop."
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
    badges.append("PRIVATE 🔒" if st.get("private") else "PUBLIC")
    if st.get("status"):
        badges.append(str(st["status"]))
    return (
        f"🔴 **{u}**\n\n"
        f"{'📡 **LIVE**' if st.get('online') else '📵'}"
        f" | 👀 `{st.get('viewers', 0)}` | 🌍 `{st.get('country') or '??'}`\n"
        f"🏷 `{' | '.join(badges)}` | id `{st.get('id') or '?'}`\n\n"
        + ("⚠️ Private/group show — public HLS nahi, record nahi hoga.\n"
           if st.get("private") else "Quality + duration choose karo:")
    )


def _card_kb(st: dict):
    u = st["username"]
    rows = []
    if st.get("online") and not st.get("private"):
        rows.append([
            InlineKeyboardButton("1 min", callback_data=f"sc:q:{u}:60"),
            InlineKeyboardButton("5 min", callback_data=f"sc:q:{u}:300"),
            InlineKeyboardButton("10 min", callback_data=f"sc:q:{u}:600"),
        ])
        rows.append([
            InlineKeyboardButton("30 min", callback_data=f"sc:q:{u}:1800"),
            InlineKeyboardButton("♾ Until Stop", callback_data=f"sc:q:{u}:0"),
        ])
    rows.append([
        InlineKeyboardButton("🔄 Refresh", callback_data=f"sc:card:{u}"),
        InlineKeyboardButton("✖️ Close", callback_data="sc:close"),
    ])
    return InlineKeyboardMarkup(rows)


def _qual_kb(model: str, dur: int):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Source", callback_data=f"sc:rec:{model}:{dur}:source"),
            InlineKeyboardButton("480p", callback_data=f"sc:rec:{model}:{dur}:480p"),
            InlineKeyboardButton("240p", callback_data=f"sc:rec:{model}:{dur}:240p"),
        ],
        [InlineKeyboardButton("⬅️ Back", callback_data=f"sc:card:{model}")],
    ])


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
            await wait.edit_text(f"❌ Status fail: `{str(e)[:200]}`")
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
    km = load_key_map()
    extra = f"\n🔑 Keys loaded: **{len(km)}** pair(s)" if is_owner(uid) else ""
    await m.reply_text(HELP + extra, disable_web_page_preview=True)


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
        return await m.reply_text("Usage: `/rec ModelName` ya link paste karo.")
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
    rec_id = _USER_ACTIVE.get(uid)
    rec = _RECS.get(rec_id) if rec_id else None
    if not rec:
        return await m.reply_text("Koi recording nahi chal rahi.")
    rec["stop"].set()
    await m.reply_text("🛑 Stop — finalize + upload ho raha hai…")


async def cmd_stat(client: Client, m: Message):
    uid = m.from_user.id if m.from_user else 0
    rec_id = _USER_ACTIVE.get(uid)
    rec = _RECS.get(rec_id) if rec_id else None
    if not rec:
        return await m.reply_text("Idle — koi rec nahi.")
    el = int(time.time() - rec.get("t0", time.time()))
    await m.reply_text(
        f"🔴 **{rec.get('model')}** recording\n"
        f"⏱ `{fmt_dur(el)}` | id `{rec_id}`"
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


async def on_paste(client: Client, m: Message):
    uid = m.from_user.id if m.from_user else 0
    if not allowed(uid):
        return await m.reply_text(deny_text())
    text = m.text or ""
    urls = URL_RE.findall(text)
    model = ""
    for u in urls:
        if is_stripchat_url(u):
            model = model_from_input(u)
            break
    if not model:
        model = model_from_input(text.strip())
        if not model:
            return
        # bare username only if it looks like a handle, not a sentence
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
        rows.append([InlineKeyboardButton(
            f"🔴 {mdl.get('username')} ({mdl.get('viewersCount', 0)})",
            callback_data=f"sc:card:{mdl.get('username')}",
        )])
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"sc:pg:{tag}:{max(0, offset-step)}"))
    if offset + step < total and models:
        nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"sc:pg:{tag}:{offset+step}"))
    if nav:
        rows.append(nav)
    rows.append([
        InlineKeyboardButton(t.upper() if t != tag else f"✅ {t.upper()}",
                             callback_data=f"sc:pg:{t}:0")
        for t in TAGS
    ])
    rows.append([InlineKeyboardButton("✖️ Close", callback_data="sc:close")])
    return InlineKeyboardMarkup(rows)


# ---------- record task ----------
async def _upload_one(client, status_msg, uid, path, model, idx, total):
    w, h, dur = probe_video(path)
    thumb = os.path.splitext(path)[0] + "_th.jpg"
    tpath = await make_thumb(path, thumb)
    title = f"{model} LIVE {time.strftime('%d-%b %H:%M')}"
    started = time.time()
    last = [0.0]

    async def _prog(cur, tot):
        now = time.time()
        if now - last[0] < 3:
            return
        last[0] = now
        el = max(now - started, 0.001)
        try:
            await status_msg.edit_text(
                f"📤 **Uploading…** `{humanbytes(cur)}/{humanbytes(tot)}` "
                f"({humanbytes(int(cur / el))}/s)\n🎬 {title}"
                + (f" [Part {idx}/{total}]" if total > 1 else "")
            )
        except Exception:
            pass

    cap = f"🎥 **{title}**\n🔴 Stripchat LIVE"
    if total > 1:
        cap += f" | Part {idx}/{total}"
    await client.send_video(
        chat_id=uid, video=path, caption=cap,
        duration=max(dur, 1), width=w or 0, height=h or 0,
        supports_streaming=True,
        thumb=tpath if tpath else None,
        progress=_prog,
    )
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
    stop = _RECS[rec_id]["stop"]
    work = os.path.join(config.DOWNLOAD_DIR, str(uid), rec_id)
    os.makedirs(work, exist_ok=True)
    reason = "error"
    try:
        async with aiohttp.ClientSession() as session:
            st = await fetch_model_status(session, model)
            if not st.get("id"):
                raise MouflonError("Stream id nahi mila (offline/typo?).")
            if st.get("private"):
                raise MouflonError("Private/group show — public stream nahi hai.")
            if not st.get("online"):
                raise MouflonError("Model online nahi dikh rahi.")
            model_id = int(st["id"])

            async def on_tick(info):
                left = info.get("left")
                q = info.get("quality") or quality
                res = info.get("res") or ""
                await status_msg.edit_text(
                    f"🔴 **RECORDING: {model}**\n\n"
                    f"🎞 `{q}` {('('+res+')') if res else ''}\n"
                    f"⏱ Elapsed: `{fmt_dur(info['elapsed'])}`"
                    + (f" | ⏳ Left: `{fmt_dur(left)}`\n" if left is not None else " | ♾ Until Stop\n")
                    + f"💾 `{humanbytes(info['bytes'])}` | 🧩 parts `{info['parts']}`\n"
                    f"📶 `{humanbytes(int(info['bytes']/max(info['elapsed'],1)))}/s`\n\n"
                    f"⏹ Stop dabao:",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("⏹ STOP & UPLOAD", callback_data=f"sc:stop:{rec_id}")]]
                    ),
                )

            parts, reason, total_bytes, chosen = await record_to_parts(
                session, model, model_id, quality, dur_seconds, stop, work,
                until_stop_cap=config.UNTIL_STOP_CAP_MIN * 60,
                on_tick=on_tick,
            )
    except MouflonError as e:
        try:
            await status_msg.edit_text(f"❌ **Recording nahi hui:**\n{e}")
        except Exception:
            pass
        cleanup_dir(work)
        _RECS.pop(rec_id, None)
        _USER_ACTIVE.pop(uid, None)
        return
    except Exception as e:
        logger.exception("record crash")
        try:
            await status_msg.edit_text(f"❌ **Error:** `{str(e)[:300]}`")
        except Exception:
            pass
        cleanup_dir(work)
        _RECS.pop(rec_id, None)
        _USER_ACTIVE.pop(uid, None)
        return

    label = {"stopped": "🛑 stopped", "offline": "📴 stream ended", "duration": "⏱ done"}.get(reason, reason)
    if not parts:
        try:
            await status_msg.edit_text(f"⚠️ **{model}** — kuch capture nahi hua ({label}).")
        except Exception:
            pass
        cleanup_dir(work)
        _RECS.pop(rec_id, None)
        _USER_ACTIVE.pop(uid, None)
        return

    finals = []
    for i, p in enumerate(parts, 1):
        try:
            await status_msg.edit_text(f"🎞 **Remux…** {i}/{len(parts)}")
        except Exception:
            pass
        finals.append(await remux_to_mp4(p))

    ok = 0
    for i, path in enumerate(finals, 1):
        try:
            await status_msg.edit_text(f"📤 **Uploading…** {i}/{len(finals)}")
            await _upload_one(client, status_msg, uid, path, model, i, len(finals))
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
        await client.send_message(
            uid,
            f"✅ **{model}** — {ok}/{len(finals)} uploaded ({label})."
            if ok else f"❌ **{model}** upload fail ({label}).",
        )
    except Exception:
        pass
    cleanup_dir(work)
    _RECS.pop(rec_id, None)
    _USER_ACTIVE.pop(uid, None)


async def start_recording(client, c: CallbackQuery, model: str, dur: int, quality: str):
    uid = c.from_user.id
    if not allowed(uid):
        return await c.answer("Access nahi.", show_alert=True)
    if uid in _USER_ACTIVE:
        return await c.answer("Pehle current rec Stop karo.", show_alert=True)
    if len(_RECS) >= config.MAX_CONCURRENT_REC:
        return await c.answer("Server busy — max recordings full.", show_alert=True)
    status_msg = await c.message.reply_text(f"⏳ **{model}** start ho rahi hai… (`{quality}`)")
    rec_id = f"r{uid}_{secrets.token_hex(3)}"
    _RECS[rec_id] = {"stop": asyncio.Event(), "user_id": uid, "model": model, "t0": time.time()}
    _USER_ACTIVE[uid] = rec_id
    asyncio.create_task(_record_task(client, rec_id, uid, c.from_user, model, dur, quality, status_msg))
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
    await m.reply_text("pong ✅ bot alive")


_KNOWN_CMDS = {
    "start", "help", "ping", "rec", "str", "record", "cam",
    "live", "top", "strtop", "browse", "stop", "mystat", "status", "recstatus", "keys",
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
    return await m.reply_text("Unknown command.\n\n" + HELP, disable_web_page_preview=True)


def register(app: Client):
    """Instance pe handlers lagao (class @Client decorator Koyeb pe silent tha)."""
    priv = filters.private
    app.add_handler(MessageHandler(cmd_start, priv & _cmd("start", "help")))
    app.add_handler(MessageHandler(cmd_ping, priv & _cmd("ping")))
    app.add_handler(MessageHandler(cmd_rec, priv & _cmd("rec", "str", "record", "cam")))
    app.add_handler(MessageHandler(cmd_live, priv & _cmd("live", "top", "strtop", "browse")))
    app.add_handler(MessageHandler(cmd_stop, priv & _cmd("stop")))
    app.add_handler(MessageHandler(cmd_stat, priv & _cmd("mystat", "status", "recstatus")))
    app.add_handler(MessageHandler(cmd_keys, priv & _cmd("keys")))
    app.add_handler(MessageHandler(on_paste, priv & filters.text & ~filters.regex(r"^/")))
    app.add_handler(CallbackQueryHandler(on_cb, filters.regex(r"^sc:")))
    # last: unknown slash commands
    app.add_handler(MessageHandler(on_unknown, priv & filters.text), group=8)
    logger.info("handlers registered on client")
