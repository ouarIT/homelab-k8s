"""
common.py
---------
Todo lo que comparten scraper.py y discovery.py:

- Config vía variables de entorno (única fuente: el ConfigMap generado por Kustomize).
- Cliente de IA (chat completions, compatible con Ollama detrás de Cloudflare Access).
- ensure_profile(): convierte tu CV en un perfil de búsqueda estructurado
  (search_terms, exclude_titles, competencias) UNA sola vez por versión de CV,
  para que dejes de tener que adivinar palabras clave a mano.
- Blocklist de dominios persistente, para que el discovery sea de bajo perfil:
  un dominio que te bloquea dos veces, se marca y no se vuelve a tocar.
- Utilidades de fetch respetando robots.txt, y notificación a Gotify.
"""

import hashlib
import json
import os
import random
import re
import time
import urllib.robotparser
from urllib.parse import urlparse

import requests

from cv_extract import latex_to_text

# ---------------- Rutas ----------------

DATA_DIR = "/data"
CV_FILE = os.environ.get("CV_FILE", "/app/cv.txt")

CV_CLEAN_FILE = os.path.join(DATA_DIR, "cv_clean.txt")
CV_HASH_FILE = os.path.join(DATA_DIR, "cv_hash.txt")
PROFILE_FILE = os.path.join(DATA_DIR, "profile.json")
DOMAIN_BLOCKLIST_FILE = os.path.join(DATA_DIR, "domain_blocklist.json")
SCAN_HISTORY_FILE = os.path.join(DATA_DIR, "scan_history.json")

# ---------------- Config general ----------------

def get_env_list(name: str, default: list) -> list:
    val = os.environ.get(name, "")
    if not val:
        return default
    return [item.strip() for item in val.split(",") if item.strip()]

LOCATION = os.environ.get("LOCATION", "Mexico")
LOCATION_ALLOW = get_env_list("LOCATION_ALLOW", ["san luis potosi", "san luis potosí", "slp"])
REMOTE_HINTS = get_env_list("REMOTE_HINTS", ["remote", "remoto", "home office", "hybrid", "híbrido", "desde casa"])
MIN_MATCH_SCORE = int(os.environ.get("MIN_MATCH_SCORE", "40"))

GOTIFY_URL = os.environ.get("GOTIFY_URL", "")
GOTIFY_TOKEN = os.environ.get("GOTIFY_TOKEN", "")
JOBS_PUBLIC_URL = os.environ.get("JOBS_PUBLIC_URL", "http://jobs.ouarit.lol")

IA_URL = os.environ.get("IA_URL", "")
IA_MODEL = os.environ.get("IA_MODEL", "qwen3.5:9b")
CF_CLIENT_ID = os.environ.get("CF_CLIENT_ID", "")
CF_CLIENT_SECRET = os.environ.get("CF_CLIENT_SECRET", "")
IA_BEARER_TOKEN = os.environ.get("IA_BEARER_TOKEN", "")
IA_TIMEOUT_SECONDS = int(os.environ.get("IA_TIMEOUT_SECONDS", "180"))

DISCOVERY_STRIKES_BEFORE_BLOCK = int(os.environ.get("DISCOVERY_STRIKES_BEFORE_BLOCK", "2"))
DISCOVERY_MAX_DOMAINS_PER_RUN = int(os.environ.get("DISCOVERY_MAX_DOMAINS_PER_RUN", "1"))
DISCOVERY_URL_COOLDOWN_HOURS = int(os.environ.get("DISCOVERY_URL_COOLDOWN_HOURS", "24"))

USER_AGENT = os.environ.get(
    "SCRAPER_USER_AGENT",
    "Mozilla/5.0 (compatible; ouarit-jobsearch-bot/1.0; +http://jobs.ouarit.lol)",
)

# ---------------- IA client ----------------

def _ia_headers() -> dict:
    headers = {"Content-Type": "application/json"}
    if CF_CLIENT_ID and CF_CLIENT_SECRET:
        headers["CF-Access-Client-Id"] = CF_CLIENT_ID
        headers["CF-Access-Client-Secret"] = CF_CLIENT_SECRET
    if IA_BEARER_TOKEN:
        headers["Authorization"] = f"Bearer {IA_BEARER_TOKEN}"
    return headers


def _strip_markdown_json(content: str) -> str:
    content = content.strip()
    if content.startswith("```"):
        content = content.strip("`")
        if content.lower().startswith("json"):
            content = content[4:]
    return content.strip()


def ia_chat_json(prompt: str, attempts: int = 2):
    """Manda un prompt a la IA y parsea la respuesta como JSON.
    Regresa None si falla tras los intentos configurados."""
    if not IA_URL:
        return None

    payload = {
        "model": IA_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "reasoning_effort": "none",
    }

    for attempt in range(attempts):
        try:
            resp = requests.post(IA_URL, headers=_ia_headers(), json=payload, timeout=IA_TIMEOUT_SECONDS)
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            return json.loads(_strip_markdown_json(content))
        except requests.exceptions.Timeout:
            continue
        except Exception as e:
            print(f"[WARN] fallo IA (intento {attempt + 1}/{attempts}): {e}")
    return None

# ---------------- Perfil generado desde el CV ----------------

DEFAULT_PROFILE = {
    "search_terms": ["DevOps Engineer", "Site Reliability Engineer", "Infrastructure Engineer"],
    "exclude_titles": ["senior", "director", "manager", "head of", "vp "],
    "core_competencies": ["monitoring", "zabbix", "postgresql", "ansible", "python"],
    "company_search_queries": ["infrastructure engineer jobs Mexico"],
}


def _cv_hash(cv_raw: str) -> str:
    return hashlib.sha256(cv_raw.encode("utf-8")).hexdigest()


def _generate_profile(cv_clean: str) -> dict:
    prompt = f"""Eres un asistente de reclutamiento técnico. Lee el siguiente CV y genera
un perfil de búsqueda de empleo ESTRUCTURADO. No inventes habilidades que no
estén en el CV. Piensa en títulos de puesto realistas para este perfil (no
solo el título actual, también posiciones equivalentes en otras empresas).

CV:
{cv_clean}

Responde SOLO con un JSON válido (sin texto adicional, sin markdown), con
este formato exacto:
{{
  "search_terms": ["lista de 5-8 títulos de puesto a buscar en bolsas de trabajo"],
  "exclude_titles": ["palabras que indican un nivel de seniority MAYOR al del candidato, ej. director, vp"],
  "core_competencies": ["8-15 tecnologías/habilidades clave del CV, en minúsculas"],
  "company_search_queries": ["3-5 queries de búsqueda web para encontrar vacantes publicadas directamente en páginas de empresas (careers pages), basadas en las competencias del candidato y su ubicación"]
}}
"""
    result = ia_chat_json(prompt, attempts=2)
    if not result or not isinstance(result, dict) or not result.get("search_terms"):
        print("[WARN] no se pudo generar perfil vía IA, usando perfil por defecto")
        return DEFAULT_PROFILE
    for key, default_val in DEFAULT_PROFILE.items():
        result.setdefault(key, default_val)
    return result


def ensure_profile(force: bool = False) -> dict:
    """Regresa el perfil de búsqueda vigente, regenerándolo vía IA solo si
    el CV cambió desde la última corrida (o si no existe todavía)."""
    os.makedirs(DATA_DIR, exist_ok=True)

    if not os.path.exists(CV_FILE):
        print(f"[WARN] no se encontró CV en {CV_FILE}, usando perfil por defecto")
        return DEFAULT_PROFILE

    with open(CV_FILE, encoding="utf-8") as f:
        cv_raw = f.read()

    current_hash = _cv_hash(cv_raw)
    previous_hash = None
    if os.path.exists(CV_HASH_FILE):
        with open(CV_HASH_FILE) as f:
            previous_hash = f.read().strip()

    needs_regen = force or (current_hash != previous_hash) or not os.path.exists(PROFILE_FILE)

    if not needs_regen:
        with open(PROFILE_FILE, encoding="utf-8") as f:
            return json.load(f)

    print("[INFO] CV nuevo o modificado detectado, regenerando perfil de búsqueda vía IA...")
    cv_clean = latex_to_text(cv_raw)
    with open(CV_CLEAN_FILE, "w", encoding="utf-8") as f:
        f.write(cv_clean)

    profile = _generate_profile(cv_clean)

    with open(PROFILE_FILE, "w", encoding="utf-8") as f:
        json.dump(profile, f, indent=2, ensure_ascii=False)
    with open(CV_HASH_FILE, "w") as f:
        f.write(current_hash)

    print(f"[OK] Perfil regenerado: {len(profile['search_terms'])} términos de búsqueda, "
          f"{len(profile.get('company_search_queries', []))} queries de discovery")
    return profile


def load_cv_clean() -> str:
    """CV en texto plano, para usarlo en los prompts de scoring."""
    if os.path.exists(CV_CLEAN_FILE):
        with open(CV_CLEAN_FILE, encoding="utf-8") as f:
            return f.read()
    ensure_profile()
    if os.path.exists(CV_CLEAN_FILE):
        with open(CV_CLEAN_FILE, encoding="utf-8") as f:
            return f.read()
    return ""

# ---------------- Blocklist de dominios (bajo perfil) ----------------

def load_domain_blocklist() -> dict:
    if os.path.exists(DOMAIN_BLOCKLIST_FILE):
        try:
            with open(DOMAIN_BLOCKLIST_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_domain_blocklist(blocklist: dict) -> None:
    with open(DOMAIN_BLOCKLIST_FILE, "w", encoding="utf-8") as f:
        json.dump(blocklist, f, indent=2)


def domain_of(url: str) -> str:
    return urlparse(url).netloc.lower()


def is_domain_avoided(blocklist: dict, url: str) -> bool:
    entry = blocklist.get(domain_of(url))
    return bool(entry and entry.get("avoid"))


def register_strike(blocklist: dict, url: str, reason: str) -> None:
    """Registra un tropiezo (403/429/captcha) contra un dominio. Al llegar
    al umbral configurado, el dominio se marca para nunca más visitarse
    automáticamente hasta que tú lo revises a mano."""
    d = domain_of(url)
    entry = blocklist.setdefault(d, {"strikes": 0, "avoid": False})
    entry["strikes"] += 1
    entry["last_reason"] = reason
    entry["last_strike_utc"] = _utcnow_iso()
    if entry["strikes"] >= DISCOVERY_STRIKES_BEFORE_BLOCK:
        entry["avoid"] = True
        print(f"[WARN] Dominio '{d}' marcado para evitar permanentemente "
              f"({entry['strikes']} tropiezos, motivo: {reason})")


def _utcnow_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()

# ---------------- Fetch respetuoso (robots.txt) ----------------

def can_fetch(url: str) -> bool:
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    rp = urllib.robotparser.RobotFileParser()
    try:
        resp = requests.get(robots_url, headers={"User-Agent": USER_AGENT}, timeout=5)
        if resp.status_code == 200:
            rp.parse(resp.text.splitlines())
            return rp.can_fetch(USER_AGENT, url)
    except Exception:
        pass
    return True  # si robots.txt no responde, se asume permitido


def fetch_url(url: str, post_data: dict = None) -> str:
    """Regresa el HTML, o uno de estos códigos de estado:
    'ROBOTS_DISALLOW', 'RATE_LIMITED_<code>', 'CAPTCHA_DETECTED', 'ERROR'."""
    if not can_fetch(url):
        return "ROBOTS_DISALLOW"

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "es-ES,es;q=0.8,en-US;q=0.5,en;q=0.3",
    }
    try:
        if post_data:
            resp = requests.post(url, data=post_data, headers=headers, timeout=15)
        else:
            resp = requests.get(url, headers=headers, timeout=15)

        if resp.status_code in (429, 403):
            return f"RATE_LIMITED_{resp.status_code}"

        resp.raise_for_status()
        html = resp.text
        if any(marker in html.lower() for marker in ("captcha", "cloudflare-challenge", "ddg-captcha")):
            return "CAPTCHA_DETECTED"
        return html
    except Exception as e:
        print(f"[WARN] Error al acceder a {url}: {e}")
        return "ERROR"


def clean_html(html_content: str) -> str:
    text = re.sub(r'<script.*?>.*?</script>', ' ', html_content, flags=re.DOTALL)
    text = re.sub(r'<style.*?>.*?</style>', ' ', text, flags=re.DOTALL)
    text = re.sub(r'<.*?>', ' ', text, flags=re.DOTALL)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

# ---------------- Gotify ----------------

def notify_gotify(n_new: int) -> None:
    if not GOTIFY_TOKEN or not GOTIFY_URL:
        return
    try:
        requests.post(
            f"{GOTIFY_URL}/message?token={GOTIFY_TOKEN}",
            json={
                "title": "🔎 Búsqueda de empleo",
                "message": f"{n_new} vacante(s) nueva(s) matchean tu perfil. Revisa {JOBS_PUBLIC_URL}",
                "priority": 5,
            },
            timeout=10,
        )
    except Exception as e:
        print(f"[WARN] no se pudo notificar a Gotify: {e}")

# ---------------- Historial genérico (usado por discovery) ----------------

def load_scan_history() -> dict:
    if os.path.exists(SCAN_HISTORY_FILE):
        try:
            with open(SCAN_HISTORY_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"urls": {}, "engines": {}}


def save_scan_history(history: dict) -> None:
    with open(SCAN_HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)


def jittered_sleep(low: int, high: int) -> None:
    time.sleep(random.randint(low, high))
