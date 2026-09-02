# -*- coding: utf-8 -*-
"""Button UI + owner admin panel. Commands optional."""
from __future__ import annotations

import os
import gc
import time
import logging
import shutil

from pyrogram.enums import ParseMode
from pyrogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, Message, CallbackQuery,
)

import config
import monitor

logger = logging.getLogger("sc.panel")

# uid -> {a, t, ...}  a=rec|mon|amon
WAIT = {}
WAIT_TTL = 180


def set_wait(uid: int, action: str, **extra) -> None:
    WAIT[uid] = {"a": action, "t": time.time(), **extra}


def pop_wait(uid: int):
    w = WAIT.pop(uid, None)
    if not w:
        return None
    if time.time() - float(w.get("t") or 0) > WAIT_TTL:
        return None
    return w


def peek_wait(uid: int):
    w = WAIT.get(uid)
    if not w:
        return None
    if time.time() - float(w.get("t") or 0) > WAIT_TTL:
        WAIT.pop(uid, None)
        return None
    return w


def reply_kb(uid: int = 0):
    rows = [
        [KeyboardButton("📡 Live"), KeyboardButton("📌 Monitor")],
        [KeyboardButton("🔴 Record"), KeyboardButton("⏹ Stop")],
        [KeyboardButton("📊 Status"), KeyboardButton("🏠 Menu")],
    ]
    try:
        from handlers import is_owner
        if uid and is_owner(uid):
            rows.append([KeyboardButton("🔐 Admin")])
    except Exception:
        pass
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def home_text() -> str:
    return (
        "╭─ ⟨ <b>ＳＴＲＩＰＣＨＡＴ</b> ⟩ ─╮\n"
        "│  Sab <b>buttons</b> se — cmd zaroori nahi\n"
        "│\n"
        "│  📡 Live — online cams tap\n"
        "│  📌 Monitor — 80 watch · rec unlimited (safe)\n"
        "│  🔴 Record — naam / link bhejo\n"
        "│  ⏹ Stop · 📊 Status\n"
        "╰─ online ⇒ rec, gap ~1s ─╯"
    )


def home_ikb(uid: int = 0):
    from handlers import is_owner
    rows = [
        [
            InlineKeyboardButton("📡 Live now", callback_data="sc:m:live"),
            InlineKeyboardButton("📌 Monitors", callback_data="sc:m:mon"),
        ],
        [
            InlineKeyboardButton("🔴 Record", callback_data="sc:m:rec"),
            InlineKeyboardButton("⏹ Stop rec", callback_data="sc:m:stop"),
        ],
        [
            InlineKeyboardButton("📊 Status", callback_data="sc:m:stat"),
            InlineKeyboardButton("🔄 Refresh", callback_data="sc:m:home"),
        ],
    ]
    if is_owner(uid):
        rows.append([InlineKeyboardButton("🔐 Admin panel", callback_data="sc:ad:home")])
    return InlineKeyboardMarkup(rows)


def mon_ikb(uid: int, page: int = 0):
    from handlers import user_recording_model
    sl = monitor.slots(uid)
    per = 8
    page = max(0, int(page or 0))
    total = len(sl)
    pages = max(1, (total + per - 1) // per)
    if page >= pages:
        page = pages - 1
    chunk = sl[page * per:(page + 1) * per]
    rows = []
    for s in chunk:
        name = s.get("model") or ""
        rec = "🟢" if user_recording_model(uid, name) else "🔵"
        rows.append([
            InlineKeyboardButton(f"{rec} {name}", callback_data=f"sc:card:{name}"),
            InlineKeyboardButton("🗑", callback_data=f"sc:unmon:{name}"),
        ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"sc:m:mon:{page-1}"))
    if total:
        nav.append(InlineKeyboardButton(f"{page+1}/{pages}", callback_data=f"sc:m:mon:{page}"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"sc:m:mon:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([
        InlineKeyboardButton("➕ Add name", callback_data="sc:m:askmon"),
        InlineKeyboardButton("📡 Pick live", callback_data="sc:m:live"),
    ])
    if sl:
        rows.append([InlineKeyboardButton("🗑 Clear all slots", callback_data="sc:unmon:all")])
    rows.append([InlineKeyboardButton("⬅️ Home", callback_data="sc:m:home")])
    return InlineKeyboardMarkup(rows)


def _rss() -> str:
    try:
        with open("/proc/self/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    kb = int(line.split()[1])
                    if kb >= 1024:
                        return f"{kb/1024:.0f} MB"
                    return f"{kb} kB"
    except Exception:
        pass
    return "?"


def _disk() -> str:
    try:
        root = config.DOWNLOAD_DIR
        n, b = 0, 0
        if os.path.isdir(root):
            for dp, _, fns in os.walk(root):
                for fn in fns:
                    p = os.path.join(dp, fn)
                    try:
                        b += os.path.getsize(p)
                        n += 1
                    except Exception:
                        pass
        from engine import humanbytes
        return f"{n} files · {humanbytes(b)}"
    except Exception:
        return "?"


def admin_text() -> str:
    from handlers import _RECS, user_rec_count
    from engine import load_key_map, load_sc_cookie
    recs = len(_RECS)
    mons = monitor.all_watchers()
    users = {u for u, _ in mons}
    km = load_key_map()
    ck = "yes" if load_sc_cookie() else "no"
    loop = "on" if monitor._LOOP_STARTED else "off"
    lines = [
        "╭─ ⟨ <b>ＡＤＭＩＮ</b> ⟩ ─╮",
        f"│ 🎙 rec <b>{recs}</b>/{config.MAX_CONCURRENT_REC}",
        f"│ 📌 mon <b>{len(mons)}</b> · users <b>{len(users)}</b>",
        f"│ 🔁 loop <code>{loop}</code> · poll <code>{config.MONITOR_POLL}s</code>",
        f"│ 🧠 RSS <code>{_rss()}</code>",
        f"│ 💾 dl <code>{_disk()}</code>",
        f"│ 🔑 keys <b>{len(km)}</b> · cookie <code>{ck}</code>",
        "╰─ buttons se control ─╯",
    ]
    if recs:
        from engine import fmt_dur
        for rid, rec in list(_RECS.items())[:6]:
            el = int(time.time() - rec.get("t0", time.time()))
            lines.insert(-1, f"│ 🔴 {rec.get('model')} · {fmt_dur(el)} · u{rec.get('user_id')}")
    return "\n".join(lines)


def admin_ikb():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Health", callback_data="sc:ad:home"),
            InlineKeyboardButton("🎙 All recs", callback_data="sc:ad:recs"),
        ],
        [
            InlineKeyboardButton("📌 All monitors", callback_data="sc:ad:mons"),
            InlineKeyboardButton("👥 Users", callback_data="sc:ad:users"),
        ],
        [
            InlineKeyboardButton("⏹ Stop ALL rec", callback_data="sc:ad:stopall"),
            InlineKeyboardButton("🧹 RAM / disk", callback_data="sc:ad:gc"),
        ],
        [
            InlineKeyboardButton("🔑 Keys", callback_data="sc:ad:keys"),
            InlineKeyboardButton("🍪 Cookie", callback_data="sc:ad:cookie"),
        ],
        [
            InlineKeyboardButton("➕ Add my mon", callback_data="sc:m:askmon"),
            InlineKeyboardButton("📡 Live", callback_data="sc:m:live"),
        ],
        [
            InlineKeyboardButton("🗑 Clear my mons", callback_data="sc:unmon:all"),
            InlineKeyboardButton("⬅️ Home", callback_data="sc:m:home"),
        ],
    ])


def recs_text_kb():
    from handlers import _RECS
    from engine import fmt_dur
    rows = []
    lines = ["╭─ ⟨ <b>ＡＬＬ ＲＥＣ</b> ⟩ ─╮"]
    if not _RECS:
        lines.append("│  idle — koi rec nahi")
    else:
        for rid, rec in list(_RECS.items())[:12]:
            el = int(time.time() - rec.get("t0", time.time()))
            tag = "📌" if rec.get("monitor") else "🎙"
            model = rec.get("model") or "?"
            uid = rec.get("user_id")
            lines.append(f"│ {tag} <b>{model}</b> · {fmt_dur(el)} · u<code>{uid}</code>")
            rows.append([
                InlineKeyboardButton(f"⏹ {model}", callback_data=f"sc:stop:{rid}"),
            ])
    lines.append("╰───────────────╯")
    rows.append([
        InlineKeyboardButton("🔄", callback_data="sc:ad:recs"),
        InlineKeyboardButton("⏹ ALL", callback_data="sc:ad:stopall"),
        InlineKeyboardButton("⬅️ Admin", callback_data="sc:ad:home"),
    ])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def mons_text_kb():
    from handlers import user_recording_model
    lines = ["╭─ ⟨ <b>ＡＬＬ ＭＯＮ</b> ⟩ ─╮"]
    rows = []
    watchers = monitor.all_watchers()
    if not watchers:
        lines.append("│  koi slot nahi")
    else:
        for uid, s in watchers[:16]:
            name = s.get("model") or "?"
            st = s.get("last_state") or "wait"
            led = "🟢" if user_recording_model(uid, name) else ("🔵" if st == "wait" else "🟡")
            hits = int(s.get("hits") or 0)
            lines.append(f"│ {led} <b>{name}</b> · u<code>{uid}</code> · h{hits}")
            rows.append([
                InlineKeyboardButton(f"🔄 {name[:18]}", callback_data=f"sc:card:{name}"),
                InlineKeyboardButton("🗑", callback_data=f"sc:ad:um:{uid}:{name}"),
            ])
    lines.append(f"╰─ {len(watchers)} slot(s) ─╯")
    rows.append([InlineKeyboardButton("⬅️ Admin", callback_data="sc:ad:home")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def users_text() -> str:
    from handlers import _RECS, _USER_RECS
    rec_u = set(_USER_RECS.keys()) | {r.get("user_id") for r in _RECS.values()}
    mon_u = {u for u, _ in monitor.all_watchers()}
    lines = ["╭─ ⟨ <b>ＵＳＥＲＳ</b> ⟩ ─╮"]
    allu = sorted(rec_u | mon_u | set(config.ADMIN_IDS) | set(config.ALLOWED_USERS))
    if not allu:
        lines.append("│  empty")
    for u in allu[:20]:
        flags = []
        if u in config.ADMIN_IDS:
            flags.append("admin")
        if u in rec_u:
            flags.append("rec")
        n = len(monitor.slots(u))
        if n:
            flags.append(f"mon{n}")
        lines.append(f"│ <code>{u}</code> · {' '.join(flags) or '-'}")
    lines.append("╰───────────────╯")
    return "\n".join(lines)


def ram_clean() -> str:
    from handlers import _RECS
    live = set()
    for rid in _RECS:
        live.add(os.path.join(config.DOWNLOAD_DIR, str(_RECS[rid].get("user_id")), rid))
    removed = 0
    bytes_ = 0
    root = config.DOWNLOAD_DIR
    try:
        if os.path.isdir(root):
            for dp, dns, fns in os.walk(root, topdown=False):
                skip = any(dp.startswith(x) for x in live)
                if skip:
                    continue
                for fn in fns:
                    p = os.path.join(dp, fn)
                    try:
                        bytes_ += os.path.getsize(p)
                        os.remove(p)
                        removed += 1
                    except Exception:
                        pass
                try:
                    if not os.listdir(dp) and os.path.abspath(dp) != os.path.abspath(root):
                        os.rmdir(dp)
                except Exception:
                    pass
    except Exception as e:
        return f"cleanup err: {e}"
    try:
        gc.collect()
    except Exception:
        pass
    from engine import humanbytes
    return f"🧹 {removed} files · {humanbytes(bytes_)} free · RSS {_rss()}"


KB_MAP = {
    "📡 Live": "live",
    "📡 Live now": "live",
    "📌 Monitor": "mon",
    "📌 Monitors": "mon",
    "🔴 Record": "rec",
    "🔴 Record now": "rec",
    "⏹ Stop": "stop",
    "⏹ Stop rec": "stop",
    "📊 Status": "stat",
    "🏠 Menu": "home",
    "🔐 Admin": "admin",
}


async def send_home(msg: Message, uid: int):
    await msg.reply_text(
        home_text(),
        reply_markup=home_ikb(uid),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
    # persistent reply keyboard (ek baar)
    try:
        await msg.reply_text("⬇️ buttons", reply_markup=reply_kb(uid))
    except Exception:
        pass


async def edit_html(c: CallbackQuery, text: str, kb):
    try:
        await c.message.edit_text(
            text, reply_markup=kb, parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception:
        try:
            await c.message.reply_text(
                text, reply_markup=kb, parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception:
            pass
