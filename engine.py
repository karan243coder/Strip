# -*- coding: utf-8 -*-
"""Stripchat + white-label live HLS (Mouflon v2) — no Telegram deps."""
from __future__ import annotations

import os
import re
import json
import time
import base64
import hashlib
import asyncio
import logging
import itertools
import shutil
import subprocess
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

import aiohttp

from config import KEY_FILE, PLAYLIST_POLL, MAX_PLAYLIST_FAILS, PART_MAX_BYTES, COOKIE_FILE, STRIPCHAT_COOKIE, ROOT, EDIT_EVERY

logger = logging.getLogger("sc.engine")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
HEADERS_PAGE = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
HEADERS_JSON = {"User-Agent": UA, "Accept": "application/json"}
HEADERS_CDN = {"User-Agent": UA, "Referer": "https://stripchat.com/", "Origin": "https://stripchat.com"}


def load_sc_cookie() -> str:
    c = (STRIPCHAT_COOKIE or "").strip()
    if c:
        return c
    path = COOKIE_FILE
    if not os.path.exists(path):
        path = os.path.join(ROOT, "cookies.txt")
    if not os.path.exists(path):
        return ""
    try:
        raw = open(path, encoding="utf-8").read().strip()
        if not raw or raw.startswith("# Netscape") or "\t" in raw:
            parts = []
            for line in raw.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                cols = line.split("\t")
                if len(cols) >= 7:
                    parts.append(f"{cols[5]}={cols[6]}")
            return "; ".join(parts)
        return raw.replace("\n", " ").strip()
    except Exception:
        return ""


def cdn_headers() -> dict:
    h = dict(HEADERS_CDN)
    ck = load_sc_cookie()
    if ck:
        h["Cookie"] = ck
    return h


def page_headers() -> dict:
    h = dict(HEADERS_PAGE)
    ck = load_sc_cookie()
    if ck:
        h["Cookie"] = ck
    return h

# Same Mouflon/doppio stack
HOST_NEEDLES = (
    "stripchat.", "xhamsterlive.", "superchatlive.", "strip.chat",
    "stripchat1.", "stripchat2.",
)
PAGE_HOSTS = (
    "stripchat.com",
    "superchatlive.com",
    "xhamsterlive.com",
    "hu.stripchat.com",
)
CDN_HOSTS = (
    "doppiocdn.com",
    "doppiocdn.net",
    "doppiocdn.org",
    "doppiocdn.media",
    "doppiocdn.live",
)
PRIVATE_STATUSES = frozenset({
    "private", "groupShow", "p2p", "virtualPrivate", "p2pVoice", "offline",
})
TAGS = ("girls", "couples", "guys", "trans")
LIST_API = "https://stripchat.com/api/front/models"
CAM_API = "https://stripchat.com/api/front/v2/models/username/{u}/cam"


class MouflonError(Exception):
    pass


def humanbytes(n) -> str:
    try:
        n = float(n)
    except Exception:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} PB"


def fmt_dur(secs: int) -> str:
    secs = max(0, int(secs))
    return f"{secs // 3600:02d}:{(secs % 3600) // 60:02d}:{secs % 60:02d}"


def is_stripchat_url(url: str) -> bool:
    try:
        h = (urlparse(url).hostname or "").lower()
        return any(n in h for n in HOST_NEEDLES)
    except Exception:
        return False


def model_from_input(text: str) -> str:
    t = (text or "").strip().strip("<>")
    if not t:
        return ""
    if t.startswith("http://") or t.startswith("https://") or "://" in t:
        try:
            p = urlparse(t.split()[0])
            host = (p.hostname or "").lower()
            if any(n in host for n in HOST_NEEDLES):
                seg = [x for x in p.path.strip("/").split("/") if x]
                skip = {"api", "tags", "search", "models", "cam", "login", "signup"}
                if seg and seg[0].lower() not in skip:
                    return seg[0]
        except Exception:
            pass
        return ""
    if re.fullmatch(r"[A-Za-z0-9_\-.]{2,40}", t):
        return t
    return ""


def _json_object_at(s: str, start: int = 0):
    i = s.find("{", start)
    if i < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for j in range(i, min(len(s), i + 900000)):
        c = s[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(s[i:j + 1])
                except Exception:
                    return None
    return None


async def aget(session, url, headers=None, timeout=25, binary=False):
    h = page_headers()
    if headers:
        h.update(headers)
    try:
        async with session.get(
            url, headers=h, timeout=aiohttp.ClientTimeout(total=timeout),
            allow_redirects=True, ssl=False,
        ) as r:
            data = await r.read()
            if binary:
                return r.status, data
            return r.status, data.decode("utf-8", "ignore")
    except Exception as e:
        logger.debug("GET fail %s: %s", url[:90], e)
        return 0, None


def load_key_map() -> dict:
    m = {}
    pk = os.environ.get("STRIPCHAT_PKEY", "").strip()
    pd = os.environ.get("STRIPCHAT_PDKEY", "").strip()
    if pk and pd:
        m[pk] = pd
    try:
        if os.path.exists(KEY_FILE):
            with open(KEY_FILE, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or ":" not in line:
                        continue
                    a, b = (x.strip() for x in line.split(":", 1))
                    if a and b and a not in m:
                        m[a] = b
    except Exception as e:
        logger.warning("keyfile read: %s", e)
    return m


def xor_b64_rev_decode(encrypted_b64_reversed: str, pdkey: str) -> str:
    hb = hashlib.sha256(pdkey.encode("utf-8")).digest()
    data = base64.b64decode(encrypted_b64_reversed[::-1] + "==")
    return bytes(a ^ b for a, b in zip(data, itertools.cycle(hb))).decode("utf-8")


def decode_segment_uri(mouflon_uri: str, pdkey: str) -> str:
    enc = mouflon_uri.split("_")[-2]
    dec = xor_b64_rev_decode(enc, pdkey)
    return mouflon_uri.replace(enc, dec)


def extract_psch(master_text: str, known_pkeys=None):
    entries = []
    for line in master_text.splitlines():
        if line.strip().upper().startswith("#EXT-X-MOUFLON:PSCH"):
            parts = line.split(":")
            if len(parts) >= 2:
                entries.append((parts[-2].strip(), parts[-1].strip()))
    if not entries:
        return None, None
    known = set(known_pkeys or [])
    if known:
        for ver, pk in entries:
            if pk in known:
                return ver, pk
    for ver, pk in entries:
        if ver.lower() == "v2":
            return ver, pk
    return entries[0]


def parse_variants(master_text: str):
    """Return list of {name, res, url} in playlist order (source first)."""
    out = []
    lines = master_text.splitlines()
    for i, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        name = "source"
        res = ""
        m = re.search(r'NAME="([^"]+)"', line)
        if m:
            name = m.group(1)
        m = re.search(r"RESOLUTION=(\d+x\d+)", line)
        if m:
            res = m.group(1)
        url = ""
        if i + 1 < len(lines) and lines[i + 1].strip().startswith("http"):
            url = lines[i + 1].strip()
        if url:
            out.append({"name": name, "res": res, "url": url})
    if not out:
        for line in lines:
            if line.strip().startswith("http"):
                out.append({"name": "source", "res": "", "url": line.strip()})
    return out


def add_query(url: str, **params) -> str:
    p = urlparse(url)
    q = dict(parse_qsl(p.query, keep_blank_values=True))
    q.update({k: str(v) for k, v in params.items() if v is not None})
    return urlunparse(p._replace(query=urlencode(q)))


# ---------------- status / browse ----------------
async def fetch_online_models(session, tag="girls", limit=40, offset=0):
    if tag not in TAGS:
        tag = "girls"
    url = f"{LIST_API}?limit={limit}&offset={offset}&primaryTag={tag}"
    code, txt = await aget(session, url, headers=HEADERS_JSON)
    if code != 200 or not txt:
        return [], 0
    try:
        d = json.loads(txt)
        return d.get("models", []) or [], int(d.get("totalCount", 0) or 0)
    except Exception:
        return [], 0


def _fill_from_preload(html: str, username: str, st: dict):
    marker = "__PRELOADED_STATE__"
    idx = html.find(marker)
    data = _json_object_at(html, idx) if idx >= 0 else None
    model = None
    if isinstance(data, dict):
        vc = data.get("viewCam") or {}
        model = vc.get("model") or {}
        cam = vc.get("cam") if isinstance(vc.get("cam"), dict) else {}
        if model:
            st["username"] = model.get("username") or st["username"]
            try:
                st["id"] = int(model.get("id") or st["id"] or 0)
            except Exception:
                pass
            st["online"] = bool(model.get("isLive") or model.get("isOnline"))
            status = str(model.get("status") or cam.get("status") or "")
            if status:
                st["status"] = status
            if status in PRIVATE_STATUSES and status != "offline":
                st["private"] = True
            if status in ("off", "idle", "offline"):
                st["online"] = False
        sn = None
        try:
            sn = (data.get("viewCam") or {}).get("streamName")
        except Exception:
            sn = None
        if not sn and model:
            sn = model.get("id")
        if sn:
            try:
                st["id"] = int(sn)
            except Exception:
                pass
    # regex fallback
    if not st.get("id"):
        m = re.search(r'"streamName":"?(\d{5,})"?', html)
        if m:
            st["id"] = int(m.group(1))
    if "public" in re.findall(r'"status":"([^"]+)"', html)[:4]:
        if not st.get("private"):
            st["status"] = st.get("status") or "public"
    lives = re.findall(r'"isLive":(true|false)', html)
    if lives and lives[0] == "true":
        st["online"] = True
    statuses = re.findall(r'"status":"([^"]+)"', html)
    for s in statuses[:8]:
        if s in PRIVATE_STATUSES:
            st["private"] = True
            st["status"] = s
            break
        if s == "public":
            st["status"] = "public"
            break
    # preview thumb
    m = re.search(r'"previewUrlThumbSmall":"(https?:[^"]+)"', html)
    if m:
        st["preview"] = m.group(1).replace("\\u002F", "/").replace("\\/", "/")
    m = re.search(r'"country":"([A-Za-z]{2})"', html)
    if m:
        st["country"] = m.group(1).upper()
    m = re.search(r'"viewersCount":(\d+)', html)
    if m:
        st["viewers"] = int(m.group(1))
    return st


async def fetch_model_status(session, username: str) -> dict:
    st = {
        "username": username, "id": 0, "online": False, "private": False,
        "viewers": 0, "country": "", "hd": False, "vr": False,
        "preview": "", "status": "", "error": "",
    }
    # 1) cam API (often 418 from datacenter IPs — ignore)
    try:
        code, txt = await aget(session, CAM_API.format(u=username), headers=HEADERS_JSON, timeout=12)
        if code == 200 and txt:
            d = json.loads(txt)
            if (d or {}).get("error"):
                st["error"] = str(d["error"])
            cam = (d or {}).get("cam") or {}
            user = ((d or {}).get("user") or {}).get("user") or {}
            if cam.get("show"):
                st["private"] = True
            try:
                st["id"] = int(cam.get("streamName") or 0)
            except Exception:
                pass
            if user.get("username"):
                st["username"] = user["username"]
            if user.get("isLive") or cam.get("isCamAvailable"):
                st["online"] = True
    except Exception:
        pass

    # 2) HTML preload (most reliable for WL domains)
    if not st["id"] or not st["online"]:
        for host in PAGE_HOSTS:
            code, html = await aget(session, f"https://{host}/{username}", timeout=20)
            if code == 200 and html and len(html) > 8000:
                _fill_from_preload(html, username, st)
                if st.get("id"):
                    break

    # 3) HLS master ping — confirms public live
    if st.get("id"):
        master = await fetch_master(session, int(st["id"]))
        if master:
            st["online"] = True
            inf = re.findall(r'RESOLUTION=(\d+x\d+).*NAME="([^"]+)"', master)
            if inf:
                st["hd"] = any(int(r.split("x")[0]) >= 720 for r, _n in inf)
                st["resolutions"] = inf
        else:
            # master missing often means offline (or geo). Don't force online.
            pass
    return st


async def fetch_master(session, model_id: int) -> str | None:
    for host in CDN_HOSTS:
        url = f"https://edge-hls.{host}/hls/{model_id}/master/{model_id}_auto.m3u8"
        code, txt = await aget(session, url, headers=cdn_headers(), timeout=12)
        if code == 200 and txt and "#EXT" in txt:
            return txt
    return None


async def is_hls_live(session, model_id: int, timeout: float = 3.0) -> bool:
    return await is_public_live(session, model_id, timeout=timeout)


async def is_public_live(session, model_id: int, timeout: float = 2.2) -> bool:
    """True only if PUBLIC HLS opens (variant 200 + Mouflon URI). 403/404/group = False."""
    try:
        mid = int(model_id or 0)
    except Exception:
        return False
    if mid <= 0:
        return False
    master = None
    for host in CDN_HOSTS[:3]:
        url = f"https://edge-hls.{host}/hls/{mid}/master/{mid}_auto.m3u8"
        try:
            code, txt = await aget(session, url, headers=cdn_headers(), timeout=timeout)
        except Exception:
            continue
        if code in (401, 402, 403):
            return False
        if code == 200 and isinstance(txt, str) and "#EXT" in txt:
            master = txt
            break
    if not master:
        return False
    if "MOUFLON-ADVERT" in master and "MOUFLON:URI:" not in master:
        return False
    variants = parse_variants(master)
    if not variants:
        return False
    psch, pkey = extract_psch(master)
    vurl = variants[0]["url"]
    if psch and pkey:
        vurl = add_query(vurl, psch=psch, pkey=pkey)
    try:
        code, pl = await aget(session, vurl, headers=cdn_headers(), timeout=timeout)
    except Exception:
        return False
    if code in (401, 402, 403, 404) or code != 200 or not pl:
        return False
    if "MOUFLON-ADVERT" in pl and "MOUFLON:URI:" not in pl:
        return False
    return "MOUFLON:URI:" in pl or "#EXT-X-MOUFLON:URI:" in pl


async def playlist_and_keys(session, model_id: int, quality: str = "source"):
    master = await fetch_master(session, model_id)
    if not master:
        raise MouflonError("Master playlist nahi mili — model offline/hidden ho sakti hai.")
    psch, pkey = extract_psch(master, known_pkeys=load_key_map().keys())
    if not pkey:
        raise MouflonError("PSCH/pkey playlist me nahi mila.")
    variants = parse_variants(master)
    if not variants:
        raise MouflonError("Master playlist me variants nahi mile.")
    want = (quality or "source").lower().replace(" ", "")
    chosen = None
    for v in variants:
        if v["name"].lower().replace(" ", "") == want:
            chosen = v
            break
    def _by_res(pred):
        for v in variants:
            r = v.get("res") or ""
            try:
                w = int(r.split("x")[0])
            except Exception:
                w = 0
            name = v["name"].lower()
            if pred(w, name):
                return v
        return None
    if chosen is None:
        if want in ("source", "best", "orig", "hd"):
            chosen = variants[0]
        elif want in ("720", "720p"):
            chosen = _by_res(lambda w, n: w == 720 or "720" in n) or variants[0]
        elif want in ("480", "480p"):
            chosen = _by_res(lambda w, n: w == 480 or "480" in n)
        elif want in ("360", "360p"):
            chosen = _by_res(lambda w, n: w == 360 or "360" in n)
        elif want in ("240", "240p"):
            chosen = _by_res(lambda w, n: w == 240 or "240" in n)
        elif want in ("160", "160p"):
            chosen = _by_res(lambda w, n: w == 160 or "160" in n)
    if chosen is None:
        chosen = variants[0]
    return chosen["url"], psch, pkey, variants, chosen


async def get_pdkey(session, sample_enc: str, pkey: str | None = None) -> str:
    km = load_key_map()
    if pkey and pkey in km:
        pd = km[pkey]
    elif km:
        pd = next(iter(km.values()))
    else:
        raise MouflonError(
            "pdkey nahi mili. `keys.txt` me `pkey:pdkey` lines daalo "
            "ya env STRIPCHAT_PKEY / STRIPCHAT_PDKEY set karo."
        )
    try:
        test = xor_b64_rev_decode(sample_enc, pd)
        if not re.fullmatch(r"[A-Za-z0-9\-_]{6,40}", test or ""):
            raise ValueError("bad decode")
    except Exception:
        raise MouflonError("pdkey stale/invalid hai. keys.txt update karo (pkey:pdkey).")
    return pd


async def fetch_live_segments(session, variant_url, psch, pkey, pdkey):
    vurl = add_query(variant_url, psch=psch, pkey=pkey)
    code, pl = await aget(session, vurl, headers=cdn_headers(), timeout=15)
    if code == 403:
        raise MouflonError("PRIVATE")
    if code == 404:
        raise MouflonError("OFFLINE")
    if code != 200 or not pl:
        raise MouflonError("OFFLINE")
    if "MOUFLON-ADVERT" in pl or ("ENDLIST" in pl and "MOUFLON:URI:" not in pl):
        return None, {}, True
    if "ENDLIST" in pl and "MOUFLON:URI:" in pl:
        # still capture remaining? treat as ending
        pass
    init_url, segs = None, {}
    last_real, last_seq = None, None
    for line in pl.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-MAP:"):
            m = re.search(r'URI="([^"]+)"', line)
            if m:
                init_url = m.group(1)
        elif line.startswith("#EXT-X-MOUFLON:URI:"):
            uri = line[len("#EXT-X-MOUFLON:URI:"):]
            try:
                last_real = decode_segment_uri(uri, pdkey)
                sm = re.search(r"_(\d+)_", uri)
                last_seq = int(sm.group(1)) if sm else None
            except Exception:
                last_real, last_seq = None, None
        elif line.endswith("media.mp4") and last_real:
            segs[last_seq if last_seq is not None else last_real] = last_real
            last_real, last_seq = None, None
        elif line.startswith("http") and not line.endswith("media.mp4"):
            sm = re.search(r"_(\d+)_", line)
            segs[int(sm.group(1)) if sm else line] = line
    if init_url:
        init_url = add_query(init_url, psch=psch, pkey=pkey)
    segs = {k: add_query(v, psch=psch, pkey=pkey) for k, v in segs.items()}
    endlist = "ENDLIST" in pl and "MOUFLON-ADVERT" in pl
    return init_url, segs, endlist


# ---------------- remux / probe / thumb ----------------
async def remux_to_mp4(src_path: str) -> str:
    out = os.path.splitext(src_path)[0] + ".mp4"
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", src_path, "-c", "copy", "-movflags", "+faststart", out,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await asyncio.wait_for(proc.communicate(), timeout=900)
        if proc.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 1024:
            try:
                os.remove(src_path)
            except Exception:
                pass
            return out
        logger.warning("ffmpeg remux fail: %r", (err or b"")[-200:])
    except Exception as e:
        logger.warning("remux: %s", e)
    try:
        if src_path != out:
            os.replace(src_path, out)
    except Exception:
        return src_path
    return out


def probe_video(path: str):
    """Return (width, height, duration_int)."""
    try:
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration", "-show_entries", "stream=codec_type,width,height",
             "-of", "json", path],
            capture_output=True, text=True, timeout=30,
        )
        d = json.loads(p.stdout or "{}")
        dur = int(float((d.get("format") or {}).get("duration") or 0))
        w = h = 0
        for st in d.get("streams") or []:
            if st.get("codec_type") == "video":
                w = int(st.get("width") or 0)
                h = int(st.get("height") or 0)
                break
        return w, h, dur
    except Exception:
        return 0, 0, 0


async def make_thumb(path: str, dest: str) -> str | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-ss", "1", "-i", path, "-frames:v", "1", "-q:v", "3", dest,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=40)
        if proc.returncode == 0 and os.path.exists(dest) and os.path.getsize(dest) > 200:
            return dest
    except Exception:
        pass
    return None


def cleanup_dir(path: str):
    if path and os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
    try:
        import gc
        gc.collect()
    except Exception:
        pass


async def record_to_parts(session, model: str, model_id: int, quality: str,
                          dur_seconds: int, stop_event: asyncio.Event,
                          work_dir: str, until_stop_cap: int,
                          on_tick=None):
    """
    Poll live HLS, append fMP4 fragments. Returns
    (part_paths: list[str], reason: str, total_bytes: int)
    """
    variant_url, psch, pkey, _vars, chosen = await playlist_and_keys(session, model_id, quality)
    vurl = add_query(variant_url, psch=psch, pkey=pkey)
    code, pl0 = await aget(session, vurl, headers=cdn_headers(), timeout=15)
    if code == 403:
        raise MouflonError("PRIVATE")
    if code == 404 or code != 200 or not pl0:
        raise MouflonError("OFFLINE")
    if "MOUFLON-ADVERT" in pl0 or ("ENDLIST" in pl0 and "MOUFLON:URI:" not in pl0):
        raise MouflonError("Model abhi live nahi hai (sirf advert/preview).")
    uris = [l for l in pl0.splitlines() if l.startswith("#EXT-X-MOUFLON:URI:")]
    if not uris:
        raise MouflonError("Mouflon segment nahi mila (format change?).")
    sample_enc = uris[0].split("_")[-2]
    pdkey = await get_pdkey(session, sample_enc, pkey=pkey)

    os.makedirs(work_dir, exist_ok=True)
    started = time.time()
    cap = dur_seconds if dur_seconds else until_stop_cap
    deadline = started + cap
    total_bytes = 0
    parts_written = []
    part_idx = 0
    cur_fh = None
    cur_path = None
    cur_bytes = 0
    done_keys = set()
    init_written = False
    init_url_global = [None]
    fails = 0
    last_tick = 0.0

    def _open_part():
        nonlocal cur_fh, cur_path, cur_bytes, init_written, part_idx
        cur_path = os.path.join(work_dir, f"{model}_{part_idx + 1:02d}.m4s")
        cur_fh = open(cur_path, "wb")
        cur_bytes = 0
        init_written = False

    def _close_part():
        nonlocal part_idx, cur_fh
        if cur_fh:
            try:
                cur_fh.close()
            except Exception:
                pass
            if cur_path and os.path.exists(cur_path) and os.path.getsize(cur_path) > 1024:
                parts_written.append(cur_path)
                part_idx += 1
            elif cur_path and os.path.exists(cur_path):
                try:
                    os.remove(cur_path)
                except Exception:
                    pass
        cur_fh = None

    async def _write_init():
        nonlocal cur_bytes, init_written
        if init_written or not init_url_global[0] or not cur_fh:
            return
        _, data = await aget(session, init_url_global[0], headers=cdn_headers(), binary=True)
        if data:
            cur_fh.write(data)
            cur_bytes += len(data)
            init_written = True

    _open_part()
    offline = False
    private = False
    try:
        while time.time() < deadline:
            if stop_event.is_set():
                break
            try:
                init_url, segs, endlist = await fetch_live_segments(
                    session, variant_url, psch, pkey, pdkey
                )
                if init_url:
                    init_url_global[0] = init_url
                fails = 0
            except MouflonError as e:
                es = str(e)
                if es == "PRIVATE" or "403" in es:
                    private = True
                    offline = True
                    break
                if es == "OFFLINE":
                    fails += 1
                    if fails >= 3:
                        offline = True
                        break
                    await asyncio.sleep(PLAYLIST_POLL)
                    continue
                fails += 1
                logger.debug("playlist poll fail (%s) %s", fails, e)
                if fails >= MAX_PLAYLIST_FAILS:
                    offline = True
                    break
                await asyncio.sleep(PLAYLIST_POLL)
                continue
            except Exception as e:
                fails += 1
                logger.debug("playlist poll err (%s) %s", fails, e)
                if fails >= MAX_PLAYLIST_FAILS:
                    offline = True
                    break
                await asyncio.sleep(PLAYLIST_POLL)
                continue
            if endlist:
                offline = True
                break
            if not init_written:
                await _write_init()
            ordered = sorted([k for k in segs if isinstance(k, int)]) + \
                [k for k in segs if not isinstance(k, int)]
            for k in ordered:
                if k in done_keys:
                    continue
                if cur_bytes >= PART_MAX_BYTES:
                    _close_part()
                    _open_part()
                    await _write_init()
                _, data = await aget(session, segs[k], headers=cdn_headers(), binary=True, timeout=30)
                if data and len(data) > 100:
                    cur_fh.write(data)
                    done_keys.add(k)
                    total_bytes += len(data)
                    cur_bytes += len(data)
            now = time.time()
            if on_tick and now - last_tick >= EDIT_EVERY:
                last_tick = now
                el = int(now - started)
                left = int(deadline - now) if dur_seconds else None
                try:
                    await on_tick({
                        "elapsed": el, "left": left, "bytes": total_bytes,
                        "parts": part_idx + (1 if cur_bytes else 0),
                        "quality": chosen.get("name"), "res": chosen.get("res"),
                    })
                except Exception:
                    pass
            if cur_fh:
                try:
                    cur_fh.flush()
                except Exception:
                    pass
            await asyncio.sleep(PLAYLIST_POLL)
    finally:
        _close_part()

    reason = "stopped" if stop_event.is_set() else ("offline" if offline else "duration")
    return parts_written, reason, total_bytes, chosen
