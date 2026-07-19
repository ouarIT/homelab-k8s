"""
discovery.py
------------
Descubre vacantes publicadas directamente en páginas de empresas (no en
bolsas de trabajo), con un perfil deliberadamente bajo:

- Las queries de búsqueda salen del perfil generado desde tu CV
  (common.ensure_profile), no de una lista que tengas que mantener a mano.
- Solo UN dominio nuevo se visita por corrida (DISCOVERY_MAX_DOMAINS_PER_RUN).
- Cada dominio que responde con bloqueo/captcha suma un "strike"; al llegar
  al umbral se marca para evitarse permanentemente (common.register_strike).
- Respeta robots.txt siempre, con user-agent identificable (no se hace pasar
  por un browser real).
"""

import hashlib
import os
from datetime import datetime, timezone, timedelta
from urllib.parse import unquote

import pandas as pd
import requests

import common

DATA_DIR = "/data"
DISCOVERY_CSV = os.path.join(DATA_DIR, "discovery_new.csv")


def is_engine_blocked(history: dict, engine: str) -> bool:
    blocked_until = history.get("engines", {}).get(engine, {}).get("blocked_until")
    if not blocked_until:
        return False
    return datetime.now(timezone.utc) < datetime.fromisoformat(blocked_until)


def set_engine_blocked(history: dict, engine: str, hours: int = 12) -> None:
    history.setdefault("engines", {}).setdefault(engine, {})
    unblock_time = datetime.now(timezone.utc) + timedelta(hours=hours)
    history["engines"][engine]["blocked_until"] = unblock_time.isoformat()
    print(f"[WARN] Motor '{engine}' bloqueado hasta {unblock_time.isoformat()}")


def search_duckduckgo(query: str, history: dict) -> list[str]:
    if is_engine_blocked(history, "duckduckgo"):
        return []
    print(f"[INFO] Buscando en DDG: {query}")
    html = common.fetch_url("https://html.duckduckgo.com/html/", post_data={"q": query})
    if html.startswith("RATE_LIMITED") or html == "CAPTCHA_DETECTED":
        set_engine_blocked(history, "duckduckgo", 12)
        return []
    if html in ("ERROR", "ROBOTS_DISALLOW"):
        return []

    links = []
    import re
    for match in re.findall(r'href="([^"]+)"', html):
        if "uddg=" in match:
            actual_url = unquote(match.split("uddg=")[1].split("&")[0])
            if actual_url.startswith("http") and "duckduckgo" not in actual_url:
                links.append(actual_url)
    return list(dict.fromkeys(links))


def search_bing(query: str, history: dict) -> list[str]:
    if is_engine_blocked(history, "bing"):
        return []
    print(f"[INFO] Buscando en Bing: {query}")
    html = common.fetch_url(f"https://www.bing.com/search?q={requests.utils.quote(query)}")
    if html.startswith("RATE_LIMITED") or html == "CAPTCHA_DETECTED":
        set_engine_blocked(history, "bing", 12)
        return []
    if html in ("ERROR", "ROBOTS_DISALLOW"):
        return []

    links = []
    import re
    for link in re.findall(r'<h2><a\s+[^>]*href="([^"]+)"', html):
        if link.startswith("http") and "microsoft" not in link and "bing.com" not in link:
            links.append(link)
    return list(dict.fromkeys(links))


def extract_and_score_jobs(text: str, cv_clean: str, search_terms: list[str]) -> list[dict]:
    if not common.IA_URL:
        return []
    prompt = f"""Eres un extractor de datos riguroso. Analiza el texto de esta página
corporativa para extraer ofertas de empleo, y evalúa cada una contra el CV
del candidato.

Perfiles buscados: {", ".join(search_terms)}

CV del candidato:
{cv_clean}

Texto de la página:
---
{text[:4000]}
---

INSTRUCCIONES:
1. No inventes información; si un dato no está en el texto, usa null.
2. Devuelve [] si no hay vacantes que encajen con el perfil.
3. Responde SOLO con el arreglo JSON, sin markdown.

Formato:
[{{"title": "...", "job_url": "url o null", "location": "... o null",
   "company": "... o null", "description": "resumen breve",
   "match_score": 0-100, "match_reason": "razón corta"}}]
"""
    result = common.ia_chat_json(prompt, attempts=2)
    return result if isinstance(result, list) else []


def run_discovery() -> None:
    print("[INFO] Iniciando ciclo de descubrimiento web (bajo perfil)...")
    profile = common.ensure_profile()
    cv_clean = common.load_cv_clean()
    history = common.load_scan_history()
    blocklist = common.load_domain_blocklist()

    queries = profile.get("company_search_queries") or []
    candidate_urls = []
    for query in queries[:2]:  # como máximo 2 queries por corrida
        links = search_duckduckgo(query, history)
        if not links:
            links = search_bing(query, history)
        candidate_urls.extend(links)
        common.jittered_sleep(4, 7)

    candidate_urls = list(dict.fromkeys(candidate_urls))

    # Filtrar dominios ya marcados como "evitar" antes de tocar nada de ellos
    candidate_urls = [u for u in candidate_urls if not common.is_domain_avoided(blocklist, u)]

    urls_history = history.get("urls", {})
    new_jobs = []
    domains_visited_this_run = 0

    for url in candidate_urls:
        if domains_visited_this_run >= common.DISCOVERY_MAX_DOMAINS_PER_RUN:
            break

        last_scanned = urls_history.get(url, {}).get("last_scanned")
        if last_scanned:
            last_dt = datetime.fromisoformat(last_scanned)
            hours_since = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
            if hours_since < common.DISCOVERY_URL_COOLDOWN_HOURS:
                continue

        common.jittered_sleep(3, 8)
        html = common.fetch_url(url)
        domains_visited_this_run += 1  # cuenta como "visita" aunque falle, para no insistir

        if html in ("RATE_LIMITED_429", "RATE_LIMITED_403", "CAPTCHA_DETECTED"):
            common.register_strike(blocklist, url, html)
            continue
        if html in ("ERROR", "ROBOTS_DISALLOW"):
            continue

        text = common.clean_html(html)
        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        last_hash = urls_history.get(url, {}).get("last_hash")

        urls_history[url] = {
            "last_scanned": datetime.now(timezone.utc).isoformat(),
            "last_hash": content_hash,
        }

        if content_hash == last_hash:
            print(f"[INFO] Sin cambios en {url}, se omite")
            continue

        extracted = extract_and_score_jobs(text, cv_clean, profile.get("search_terms", []))
        exclude_titles = [t.lower() for t in profile.get("exclude_titles", [])]

        for job in extracted:
            title = str(job.get("title", "") or "").strip()
            if not title:
                continue
            title_lower = title.lower()
            if any(bad in title_lower for bad in exclude_titles):
                continue
            score = job.get("match_score", -1)
            if isinstance(score, (int, float)) and score < common.MIN_MATCH_SCORE:
                continue

            new_jobs.append({
                "title": title,
                "company": str(job.get("company") or "Desconocida").strip(),
                "location": str(job.get("location") or common.LOCATION).strip(),
                "site": "web_discovery",
                "date_posted": datetime.now().strftime("%Y-%m-%d"),
                "job_url": str(job.get("job_url") or url).strip(),
                "description": str(job.get("description") or "").strip(),
                "is_remote": any(
                    hint in (job.get("location", "") or "").lower() or hint in title_lower
                    for hint in common.REMOTE_HINTS
                ),
                "min_amount": None,
                "max_amount": None,
                "match_score": score,
                "match_reason": str(job.get("match_reason") or ""),
            })

    history["urls"] = urls_history
    common.save_scan_history(history)
    common.save_domain_blocklist(blocklist)

    if new_jobs:
        df = pd.DataFrame(new_jobs)
        hdr = not os.path.exists(DISCOVERY_CSV)
        df.to_csv(DISCOVERY_CSV, mode="a", index=False, header=hdr)
        print(f"[OK] Añadidas {len(df)} vacantes descubiertas a {DISCOVERY_CSV}")
    else:
        print("[INFO] No se encontraron vacantes nuevas en esta corrida "
              f"({domains_visited_this_run} dominio(s) visitado(s)).")


if __name__ == "__main__":
    run_discovery()
