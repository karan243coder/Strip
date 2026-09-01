# Stripchat Live Recorder Bot

Sirf **Stripchat family live cam record**. BIMBO / JAV / TeraBox / torrent nahi.

**Domains:** stripchat.com · superchatlive.com · xhamsterlive.com · strip.chat

## Features
- Link paste ya `/rec ModelName` → live card
- `/live` browse (girls / couples / guys / trans)
- Quality: Source / 480p / 240p
- 1 / 5 / 10 / 30 min + Until Stop
- Mouflon v2 (`keys.txt`)
- Multi-CDN HLS, ffmpeg remux + thumb, Telegram upload
- Private / group-show pe record nahi (HTTP 403)

---

## 1) GitHub pe daalo

1. GitHub pe **naya empty repo** banao (README mat add karna).
2. Zip extract karke us folder se:

```bash
git init
git add .
git commit -m "Stripchat live recorder bot"
git branch -M main
git remote add origin https://github.com/YOUR_USER/YOUR_REPO.git
git push -u origin main
```

**Mat commit karo:** `.env` (gitignore me hai). Token sirf Koyeb env me.

`keys.txt` repo me rehne do — bina iske live decrypt nahi hoga.

---

## 2) Koyeb pe deploy (GitHub se)

1. [Koyeb](https://app.koyeb.com) → **Create Service** → **GitHub** → ye repo.
2. Builder: **Dockerfile** (auto detect).
3. Region jo chaho. Instance: Nano/Micro chal jayega; record ke time RAM/CPU zyada better.
4. **Environment variables** (mandatory):

| Name | Example |
|---|---|
| `SC_BOT_TOKEN` | BotFather ka **naya** token (BIMBO wala mat dalna) |
| `SC_API_ID` | my.telegram.org se |
| `SC_API_HASH` | my.telegram.org se |
| `SC_OWNER_ID` | tumhara Telegram numeric id |

Optional:

| Name | Default / matlab |
|---|---|
| `SC_ADMIN_IDS` | extra admins, space-separated |
| `SC_ALLOWED_USERS` | whitelist user ids |
| `SC_ALLOW_ALL` | `true` = koi bhi use kare (bandwidth heavy) |
| `SC_MAX_REC` | `3` |
| `SC_UNTIL_STOP_CAP_MIN` | `180` |
| `SC_LOG_CHANNEL` | log channel id |
| `PORT` | Koyeb khud set karta hai — mat override |

5. Health check: **HTTP**, path `/`, port = Koyeb `PORT` (default 8080).
6. Deploy → Telegram me `/ping` then `/start`. Reply aana chahiye.

**Agar bot silent ho:** Koyeb Redeploy after latest push. Logs me `telegram ok @YourBot` dikhna chahiye. `/ping` = `pong`.

Koyeb disk **ephemeral** hai. Record → Telegram pe upload → local file cleanup. Restart pe pending files gayab.

---

## Local run (optional)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
# ffmpeg chahiye
cp .env.example .env   # values bharo
python bot.py
```

## Commands
`/start` `/rec Model` `/live` `/stop` `/mystat`  
Owner: `/keys`

## Access
Default: `SC_OWNER_ID` + `SC_ADMIN_IDS` + `SC_ALLOWED_USERS`.  
Public: `SC_ALLOW_ALL=true`.

## Keys
`keys.txt` — har line `pkey:pdkey`. Rotate hone pe file update karke redeploy.
