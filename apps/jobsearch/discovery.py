import json
import os
import re
import time
import hashlib
import random
import requests
import pandas as pd
from urllib.parse import unquote, urlparse
import urllib.robotparser
from datetime import datetime, timezone, timedelta

DATA_DIR = "/data"
HISTORY_FILE = os.path.join(DATA_DIR, "scan_history.json")
DISCOVERY_CSV = os.path.join(DATA_DIR, "discovery_new.csv")
CV_FILE = "/app/cv.txt"

# Environment variables matching jobsearch-config / secrets
IA_URL = os.environ.get("IA_URL", "")
IA_MODEL = os.environ.get("IA_MODEL", "qwen3.5:9b")
CF_CLIENT_ID = os.environ.get("CF_CLIENT_ID", "")
CF_CLIENT_SECRET = os.environ.get("CF_CLIENT_SECRET", "")
IA_BEARER_TOKEN = os.environ.get("IA_BEARER_TOKEN", "")
IA_TIMEOUT_SECONDS = int(os.environ.get("IA_TIMEOUT_SECONDS", "180"))

def get_env_list(name: str, default: list) -> list:
    val = os.environ.get(name, "")
    if not val: return default
    return [item.strip() for item in val.split(",") if item.strip()]

SEARCH_TERMS = get_env_list("SEARCH_TERMS", ["DevOps Engineer", "Site Reliability Engineer", "SRE"])
LOCATION = os.environ.get("LOCATION", "Mexico")
EXCLUDE_TITLE_KEYWORDS = get_env_list("EXCLUDE_TITLE_KEYWORDS", ["senior", "sr.", "director", "manager"])
REQUIRE_ANY_KEYWORDS = get_env_list("REQUIRE_ANY_KEYWORDS", ["devops", "sre", "zabbix", "sysadmin"])
REMOTE_HINTS = get_env_list("REMOTE_HINTS", ["remote", "remoto", "home office", "hybrid"])

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
]

def load_history() -> dict:
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"urls": {}, "engines": {}}

def save_history(history: dict) -> None:
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)

def is_engine_blocked(history: dict, engine: str) -> bool:
    blocked_until = history.get("engines", {}).get(engine, {}).get("blocked_until")
    if not blocked_until:
        return False
    blocked_time = datetime.fromisoformat(blocked_until)
    return datetime.now(timezone.utc) < blocked_time

def set_engine_blocked(history: dict, engine: str, hours: int = 12) -> None:
    if "engines" not in history: history["engines"] = {}
    if engine not in history["engines"]: history["engines"][engine] = {}
    unblock_time = datetime.now(timezone.utc) + timedelta(hours=hours)
    history["engines"][engine]["blocked_until"] = unblock_time.isoformat()
    print(f"[WARN] Motor '{engine}' bloqueado hasta {unblock_time.isoformat()}")

def clean_html(html_content: str) -> str:
    text = re.sub(re.compile(r'<script.*?>.*?</script>', re.DOTALL), ' ', html_content)
    text = re.sub(re.compile(r'<style.*?>.*?</style>', re.DOTALL), ' ', text)
    text = re.sub(re.compile(r'<.*?>', re.DOTALL), ' ', text)
    text = re.sub(re.compile(r'\s+'), ' ', text)
    return text.strip()

def can_fetch(url: str, user_agent: str) -> bool:
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    rp = urllib.robotparser.RobotFileParser()
    rp.set_url(robots_url)
    try:
        # Simplest approach for robots parser: fetch it inline
        resp = requests.get(robots_url, timeout=5)
        if resp.status_code == 200:
            rp.parse(resp.text.splitlines())
            return rp.can_fetch(user_agent, url)
    except Exception:
        pass
    return True # Default allow if robots.txt is unreachable/errors out

def fetch_url(url: str, post_data: dict = None) -> str:
    ua = random.choice(USER_AGENTS)
    if not can_fetch(url, ua):
        print(f"[WARN] robots.txt prohíbe el acceso a {url}")
        return "ROBOTS_DISALLOW"

    headers = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "es-ES,es;q=0.8,en-US;q=0.5,en;q=0.3"
    }
    try:
        if post_data:
            resp = requests.post(url, data=post_data, headers=headers, timeout=15)
        else:
            resp = requests.get(url, headers=headers, timeout=15)
            
        if resp.status_code in [429, 403]:
            return f"RATE_LIMITED_{resp.status_code}"
            
        resp.raise_for_status()
        html = resp.text
        if "captcha" in html.lower() or "cloudflare-challenge" in html.lower() or "ddg-captcha" in html.lower():
            return "CAPTCHA_DETECTED"
        return html
    except Exception as e:
        print(f"[WARN] Error al acceder a {url}: {e}")
        return "ERROR"

def search_duckduckgo(query: str, history: dict) -> list[str]:
    if is_engine_blocked(history, "duckduckgo"): return []
    print(f"[INFO] Buscando en DDG: {query}")
    html = fetch_url("https://html.duckduckgo.com/html/", post_data={"q": query})
    if html.startswith("RATE_LIMITED") or html == "CAPTCHA_DETECTED":
        set_engine_blocked(history, "duckduckgo", 12)
        return []
    if html in ["ERROR", "ROBOTS_DISALLOW"]: return []
    
    links = []
    for match in re.findall(r'href="([^"]+)"', html):
        if "uddg=" in match:
            actual_url = unquote(match.split("uddg=")[1].split("&")[0])
            if actual_url.startswith("http") and "duckduckgo" not in actual_url:
                links.append(actual_url)
    return list(dict.fromkeys(links))

def search_bing(query: str, history: dict) -> list[str]:
    if is_engine_blocked(history, "bing"): return []
    print(f"[INFO] Buscando en Bing: {query}")
    query_encoded = requests.utils.quote(query)
    html = fetch_url(f"https://www.bing.com/search?q={query_encoded}")
    if html.startswith("RATE_LIMITED") or html == "CAPTCHA_DETECTED":
        set_engine_blocked(history, "bing", 12)
        return []
    if html in ["ERROR", "ROBOTS_DISALLOW"]: return []
    
    links = []
    for link in re.findall(r'<h2><a\s+[^>]*href="([^"]+)"', html):
        if link.startswith("http") and "microsoft" not in link and "bing.com" not in link:
            links.append(link)
    return list(dict.fromkeys(links))

def _ia_headers() -> dict:
    headers = {"Content-Type": "application/json"}
    if CF_CLIENT_ID and CF_CLIENT_SECRET:
        headers["CF-Access-Client-Id"] = CF_CLIENT_ID
        headers["CF-Access-Client-Secret"] = CF_CLIENT_SECRET
    if IA_BEARER_TOKEN:
        headers["Authorization"] = f"Bearer {IA_BEARER_TOKEN}"
    return headers

def extract_and_score_jobs(url: str, text: str, cv_text: str) -> list[dict]:
    if not IA_URL: return []
    snippet = text[:4000]
    prompt = f"""Eres un extractor de datos sumamente riguroso. Analiza el texto de esta web corporativa para extraer ofertas de empleo.
Además, evalúa cada oferta contra el CV del candidato para generar un score.

Perfiles buscados: {", ".join(SEARCH_TERMS)}

CV del Candidato:
{cv_text}

Texto de la página:
---
{snippet}
---

INSTRUCCIONES CRÍTICAS:
1. NO inventes información. Si un dato no está en el texto, coloca null.
2. Formato EXACTO (JSON array). Devuelve [] si no hay vacantes que encajen.
3. No envuelvas la respuesta en markdown, solo devuelve el arreglo JSON directo.

Formato requerido:
[
  {{
    "title": "Título exacto",
    "job_url": "URL específica o null",
    "location": "Ubicación o null",
    "company": "Empresa o null",
    "description": "Resumen breve",
    "match_score": 85,
    "match_reason": "Razón corta del score"
  }}
]
"""
    payload = {
        "model": IA_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "reasoning_effort": "none"
    }
    
    for attempt in range(2):
        try:
            resp = requests.post(IA_URL, headers=_ia_headers(), json=payload, timeout=IA_TIMEOUT_SECONDS)
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"].strip()
            if content.startswith("```"):
                content = content.strip("`")
                if content.lower().startswith("json"):
                    content = content[4:]
            content = content.strip()
            
            extracted = json.loads(content)
            if isinstance(extracted, list):
                return extracted
            return []
        except requests.exceptions.Timeout:
            continue
        except Exception as e:
            print(f"[WARN] Fallo parsing IA en intento {attempt+1}: {e}")
    return []

def run_discovery():
    print("[INFO] Iniciando ciclo de descubrimiento web...")
    history = load_history()
    cv_text = ""
    if os.path.exists(CV_FILE):
        with open(CV_FILE, "r", encoding="utf-8") as f:
            cv_text = f.read().strip()
    
    candidate_urls = []
    # Limit queries to terms to avoid aggressive hitting
    for term in SEARCH_TERMS[:2]:
        q = f'"{term}" Mexico (site:linkedin.com/posts OR "vacante" OR "empleo" OR "careers") -inurl:job'
        links = search_duckduckgo(q, history)
        if not links:
            links = search_bing(q, history)
        candidate_urls.extend(links)
        time.sleep(random.randint(4, 7))
        
    candidate_urls = list(dict.fromkeys(candidate_urls))
    new_jobs = []
    
    urls_history = history.get("urls", {})
    
    for url in candidate_urls[:10]: # Max 10 new domains per run
        last_scanned = urls_history.get(url, {}).get("last_scanned")
        if last_scanned:
            last_dt = datetime.fromisoformat(last_scanned)
            if (datetime.now(timezone.utc) - last_dt).total_seconds() < 86400:
                continue # Skip if scanned in last 24h
                
        time.sleep(random.randint(3, 8))
        html = fetch_url(url)
        if html in ["RATE_LIMITED_429", "RATE_LIMITED_403", "CAPTCHA_DETECTED", "ERROR", "ROBOTS_DISALLOW"]:
            continue
            
        text = clean_html(html)
        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        
        last_hash = urls_history.get(url, {}).get("last_hash")
        
        urls_history[url] = {
            "last_scanned": datetime.now(timezone.utc).isoformat(),
            "last_hash": content_hash
        }
        
        if content_hash != last_hash:
            extracted = extract_and_score_jobs(url, text, cv_text)
            for job in extracted:
                title = str(job.get("title", "") or "").strip()
                if not title: continue
                title_lower = title.lower()
                if any(bad in title_lower for bad in EXCLUDE_TITLE_KEYWORDS): continue
                if not any(good in title_lower for good in REQUIRE_ANY_KEYWORDS): continue
                
                new_jobs.append({
                    "title": title,
                    "company": str(job.get("company", "") or "Desconocida").strip(),
                    "location": str(job.get("location", "") or LOCATION).strip(),
                    "site": "web_discovery",
                    "date_posted": datetime.now().strftime("%Y-%m-%d"),
                    "job_url": str(job.get("job_url", "") or url).strip(),
                    "description": str(job.get("description", "") or "").strip(),
                    "is_remote": any(hint in (job.get("location", "") or "").lower() or hint in title_lower for hint in REMOTE_HINTS),
                    "min_amount": None,
                    "max_amount": None,
                    "match_score": job.get("match_score", -1),
                    "match_reason": str(job.get("match_reason", "") or "")
                })

    history["urls"] = urls_history
    save_history(history)
    
    if new_jobs:
        df = pd.DataFrame(new_jobs)
        hdr = not os.path.exists(DISCOVERY_CSV)
        df.to_csv(DISCOVERY_CSV, mode='a', index=False, header=hdr)
        print(f"[OK] Añadidas {len(df)} vacantes descubiertas a {DISCOVERY_CSV}")
    else:
        print("[INFO] No se encontraron nuevas vacantes válidas en la web.")

if __name__ == "__main__":
    run_discovery()
