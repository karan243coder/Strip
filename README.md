# Stripchat Live Recorder Bot

**Domains:** stripchat.com · superchatlive.com · xhamsterlive.com · strip.chat

## Features
- Link paste ya `/rec ModelName` → live card
- `/live` browse (girls / couples / guys / trans)
- Quality: Source / 480p / 240p
- 1 / 5 / 10 / 30 min + Until Stop
- Mouflon v2 (`keys.txt`)
- Multi-CDN HLS, ffmpeg remux + thumb, Telegram upload
- Private / group-show comming soon 🔜 

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
   SC_MAX_REC=2
   SC_MAX_REC_PER_USER=2

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

## Buttons (main)
`/start` pe panel + neeche keyboard: **Live · Monitor · Record · Stop · Status · Menu**. Owner ko **🔐 Admin**.

Admin: health, all recs, all monitors add/remove, stop-all, RAM clean, keys, cookie, users.

Commands optional: `/rec` `/live` `/mon` `/stop` `/admin`

Koyeb pe 2 models ek saath: `SC_MAX_REC=2` (ya 3). Nano pe RAM tight ho to 1 hi rec rakho.

Monitor HLS ping ~0.8s — online aate hi rec. Telegram FloodWait pe rec **nahi rukti** (progress edit skip). Rec/upload khatam → files + `gc` se RAM wapas.

## Access
Default: `SC_OWNER_ID` + `SC_ADMIN_IDS` + `SC_ALLOWED_USERS`.  
Public: `SC_ALLOW_ALL=true`.

## Keys
`keys.txt` — har line `pkey:pdkey`. Rotate hone pe file update karke redeploy.
