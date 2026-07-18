"""
jobsearch-scraper (JobSpy)
------------------
Corre según el schedule del CronJob. Scrapea vacantes con JobSpy,
las filtra con reglas simples, les asigna un score de match contra tu CV,
absorbe vacantes web de discovery_new.csv (si las hay), guarda históricos, 
genera un index.html y notifica por Gotify.
"""

import json
import os
import time
from datetime import datetime, timezone

import pandas as pd
import requests
from jobspy import scrape_jobs

# ---------------- CONFIG: búsqueda ----------------

def get_env_list(name: str, default: list) -> list:
    val = os.environ.get(name, "")
    if not val: return default
    return [item.strip() for item in val.split(",") if item.strip()]

SEARCH_TERMS = get_env_list("SEARCH_TERMS", ["DevOps Engineer", "Site Reliability Engineer", "SRE", "Zabbix", "Infrastructure Engineer"])
LOCATION = os.environ.get("LOCATION", "Mexico")
COUNTRY_INDEED = os.environ.get("COUNTRY_INDEED", "mexico")
SITES = get_env_list("SITES", ["indeed", "linkedin", "google", "zip_recruiter"])
RESULTS_WANTED = int(os.environ.get("RESULTS_WANTED", "40"))

EXCLUDE_TITLE_KEYWORDS = get_env_list("EXCLUDE_TITLE_KEYWORDS", ["senior", "sr.", "sr ", "staff", "principal", "director", "manager", "head of", "vp ", "vice president"])
REQUIRE_ANY_KEYWORDS = get_env_list("REQUIRE_ANY_KEYWORDS", ["devops", "sre", "site reliability", "zabbix", "infrastructure", "sysadmin", "platform engineer", "sre engineer", "observability"])
LOCATION_ALLOW = get_env_list("LOCATION_ALLOW", ["san luis potosi", "san luis potosí", "slp"])
REMOTE_HINTS = get_env_list("REMOTE_HINTS", ["remote", "remoto", "home office", "hybrid", "híbrido", "work from home", "desde casa"])

# ---------------- CONFIG: rutas / IO ----------------

DATA_DIR = "/data"
SEEN_FILE = os.path.join(DATA_DIR, "seen.json")
MASTER_CSV = os.path.join(DATA_DIR, "master.csv")
NEW_CSV = os.path.join(DATA_DIR, "new_today.csv")
INDEX_HTML = os.path.join(DATA_DIR, "index.html")
DISCOVERY_CSV = os.path.join(DATA_DIR, "discovery_new.csv")
HISTORY_FILE = os.path.join(DATA_DIR, "scan_history.json")

# ---------------- CONFIG: Gotify (opcional) ----------------

GOTIFY_URL = os.environ.get("GOTIFY_URL", "")
GOTIFY_TOKEN = os.environ.get("GOTIFY_TOKEN", "")
JOBS_PUBLIC_URL = os.environ.get("JOBS_PUBLIC_URL", "http://jobs.ouarit.lol")

# ---------------- CONFIG: scoring con IA (Ollama vía Cloudflare) ----------------

CV_FILE = os.environ.get("CV_FILE", "/app/cv.txt")
IA_URL = os.environ.get("IA_URL", "")
IA_MODEL = os.environ.get("IA_MODEL", "qwen3.5:9b")
CF_CLIENT_ID = os.environ.get("CF_CLIENT_ID", "")
CF_CLIENT_SECRET = os.environ.get("CF_CLIENT_SECRET", "")
IA_BEARER_TOKEN = os.environ.get("IA_BEARER_TOKEN", "")
IA_MAX_JOBS_TO_SCORE = int(os.environ.get("IA_MAX_JOBS_TO_SCORE", "40"))
IA_TIMEOUT_SECONDS = int(os.environ.get("IA_TIMEOUT_SECONDS", "180"))


# ---------------- LÓGICA: dedup e historial ----------------

def load_seen() -> set:
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    return set()

def save_seen(seen: set) -> None:
    with open(SEEN_FILE, "w") as f:
        json.dump(sorted(seen), f)

def get_hours_old() -> int:
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE) as f:
                history = json.load(f)
                last_run = history.get("last_jobspy_run")
                if last_run:
                    dt = datetime.fromisoformat(last_run)
                    hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
                    return max(1, int(hours) + 1)
        except Exception:
            pass
    return 24

def update_last_run():
    history = {}
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE) as f:
                history = json.load(f)
        except Exception:
            pass
    history["last_jobspy_run"] = datetime.now(timezone.utc).isoformat()
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)


# ---------------- LÓGICA: filtro por reglas ----------------

def passes_filters(row) -> bool:
    title = str(row.get("title", "")).lower()
    location = str(row.get("location", "")).lower()
    desc = str(row.get("description", "")).lower()
    is_remote_flag = bool(row.get("is_remote", False))
    text = f"{title} {desc} {location}"

    if any(bad in title for bad in EXCLUDE_TITLE_KEYWORDS):
        return False
    if not any(good in title for good in REQUIRE_ANY_KEYWORDS):
        return False

    location_ok = (
        any(loc in location for loc in LOCATION_ALLOW)
        or any(hint in text for hint in REMOTE_HINTS)
        or is_remote_flag
    )
    if not location_ok:
        return False

    return True


# ---------------- LÓGICA: scraping ----------------

def scrape(hours_old: int) -> pd.DataFrame:
    frames = []
    print(f"[INFO] Scrapeando JobSpy buscando últimas {hours_old} horas...")
    for term in SEARCH_TERMS:
        try:
            jobs = scrape_jobs(
                site_name=SITES,
                search_term=term,
                google_search_term=f"{term} jobs near Mexico",
                location=LOCATION,
                results_wanted=RESULTS_WANTED,
                hours_old=hours_old,
                country_indeed=COUNTRY_INDEED,
                linkedin_fetch_description=True,
            )
            if jobs is not None and not jobs.empty:
                jobs = jobs.copy()
                jobs["search_term"] = term
                frames.append(jobs)
        except Exception as e:
            print(f"[WARN] fallo scrapeando '{term}': {e}")

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


# ---------------- LÓGICA: scoring con IA ----------------

def load_cv() -> str:
    if os.path.exists(CV_FILE):
        with open(CV_FILE, encoding="utf-8") as f:
            return f.read().strip()
    return ""

def _ia_headers() -> dict:
    headers = {"Content-Type": "application/json"}
    if CF_CLIENT_ID and CF_CLIENT_SECRET:
        headers["CF-Access-Client-Id"] = CF_CLIENT_ID
        headers["CF-Access-Client-Secret"] = CF_CLIENT_SECRET
    if IA_BEARER_TOKEN:
        headers["Authorization"] = f"Bearer {IA_BEARER_TOKEN}"
    return headers

def score_job(cv_text: str, row) -> tuple[int, str]:
    title = row.get("title", "")
    company = row.get("company", "")
    location = row.get("location", "")
    description = str(row.get("description", "") or "")[:900]

    prompt = f"""Eres un asistente que evalúa qué tan bien encaja una vacante con el perfil de un candidato.
Responde de forma directa y breve, sin razonar paso a paso ni mostrar tu proceso de pensamiento.

CV del candidato:
{cv_text}

Vacante a evaluar:
Título: {title}
Empresa: {company}
Ubicación: {location}
Descripción: {description}

Responde SOLO con un JSON válido, sin texto adicional antes o después, con este formato exacto:
{{"score": <entero 0-100>, "reason": "<una frase corta en español explicando el score>"}}
"""

    payload = {
        "model": IA_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "reasoning_effort": "none",
    }

    last_error = "sin intentos"
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

            parsed = json.loads(content)
            return int(parsed.get("score", -1)), str(parsed.get("reason", ""))
        except requests.exceptions.Timeout:
            last_error = f"timeout tras {IA_TIMEOUT_SECONDS}s"
            continue
        except Exception as e:
            return -1, f"error de scoring: {e}"

    return -1, f"error de scoring: {last_error} (2 intentos)"

def score_new_jobs(df_new: pd.DataFrame, cv_text: str) -> pd.DataFrame:
    if df_new.empty or not cv_text or not IA_URL:
        df_new["match_score"] = -1
        df_new["match_reason"] = "scoring deshabilitado (falta CV o IA_URL)"
        return df_new

    scores, reasons = [], []
    for i, (_, row) in enumerate(df_new.iterrows()):
        if i >= IA_MAX_JOBS_TO_SCORE:
            scores.append(-1)
            reasons.append("no evaluado (límite de corrida alcanzado)")
            continue
        s, r = score_job(cv_text, row)
        scores.append(s)
        reasons.append(r)
        time.sleep(1)

    df_new = df_new.copy()
    df_new["match_score"] = scores
    df_new["match_reason"] = reasons
    return df_new


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


# ---------------- HTML ----------------

def _esc(val) -> str:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return ""
    return str(val).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

def _score_class(score) -> str:
    try:
        score = int(score)
    except (TypeError, ValueError): return "score-na"
    if score < 0: return "score-na"
    if score >= 75: return "score-high"
    if score >= 50: return "score-mid"
    return "score-low"

def _job_card(row) -> str:
    title = _esc(row.get("title", "Sin título"))
    company = _esc(row.get("company", "—"))
    location = _esc(row.get("location", "—"))
    site = _esc(row.get("site", "—"))
    date_posted = _esc(row.get("date_posted", ""))
    url = _esc(row.get("job_url", "#"))
    
    salary_min = row.get("min_amount")
    salary_max = row.get("max_amount")
    salary = ""
    if pd.notna(salary_min) or pd.notna(salary_max):
        lo = f"{int(salary_min):,}" if pd.notna(salary_min) else "?"
        hi = f"{int(salary_max):,}" if pd.notna(salary_max) else "?"
        salary = f'<span class="chip salary">${lo} - ${hi}</span>'

    score = row.get("match_score", None)
    reason = _esc(row.get("match_reason", ""))
    score_html = ""
    if score is not None and not (isinstance(score, float) and pd.isna(score)) and int(score) >= 0:
        score_html = f'<div class="score {_score_class(score)}">Match: {int(score)}%</div>'
        if reason:
            score_html += f'<p class="reason">{reason}</p>'

    return f"""
<article class="card" data-search="{title.lower()} {company.lower()} {location.lower()} {site.lower()}" data-site="{site}">
  <h3><a href="{url}" target="_blank" rel="noopener">{title}</a></h3>
  {score_html}
  <div class="card-meta">
    <span class="chip company">{company}</span>
    <span class="chip">{location}</span>
    <span class="chip site">{site}</span>
    {salary}
    <span class="chip date">{date_posted}</span>
  </div>
</article>"""

def _summary_stats(df: pd.DataFrame) -> str:
    if df.empty: return ""
    by_site = df["site"].value_counts().to_dict() if "site" in df.columns else {}
    chips = "".join(f'<span class="stat-chip">{_esc(site)}: {n}</span>' for site, n in by_site.items())
    return f'<div class="stats">{chips}</div>'

def _cards_section(df: pd.DataFrame, empty_msg: str) -> str:
    if df.empty: return f'<p class="empty">{empty_msg}</p>'
    if "match_score" in df.columns:
        ordered = df.sort_values("match_score", ascending=False)
    elif "date_posted" in df.columns:
        ordered = df.sort_values("date_posted", ascending=False)
    else:
        ordered = df
    return '<div class="grid">' + "".join(_job_card(r) for _, r in ordered.iterrows()) + "</div>"

def render_html(df_new: pd.DataFrame, df_master: pd.DataFrame) -> None:
    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Vacantes filtradas · ouarit.lol</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ font-family: -apple-system, "Segoe UI", sans-serif; background:#0f1115; color:#e6e6e6;
          margin:0; padding:2rem; max-width: 1100px; margin-inline:auto; }}
  h1 {{ font-size: 1.5rem; margin-bottom: 0.2rem; }}
  h2 {{ font-size: 1.1rem; color:#9fd3ff; margin-top:2.5rem; display:flex; align-items:center; gap:0.6rem; }}
  .meta {{ color:#888; font-size:0.8rem; margin-top:0; }}
  .toolbar {{ display:flex; gap:0.6rem; margin: 1.2rem 0; flex-wrap: wrap; }}
  .toolbar input, .toolbar select {{
    background:#1a1d24; border:1px solid #2a2e37; color:#e6e6e6; padding:0.5rem 0.8rem;
    border-radius:8px; font-size:0.9rem;
  }}
  .toolbar input {{ flex: 1; min-width: 200px; }}
  .stats {{ display:flex; gap:0.5rem; flex-wrap:wrap; margin: 0.6rem 0 1rem; }}
  .stat-chip {{ background:#1a1d24; border:1px solid #2a2e37; padding:0.2rem 0.7rem; border-radius:999px; font-size:0.78rem; color:#9fd3ff; }}
  .grid {{ display:grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 0.8rem; }}
  .card {{ background:#181b22; border:1px solid #262a33; border-radius:12px; padding:1rem 1.1rem; }}
  .card h3 {{ margin:0 0 0.5rem; font-size: 0.98rem; line-height:1.3; }}
  .card a {{ color:#e6e6e6; text-decoration:none; }}
  .card a:hover {{ color:#6cc6ff; }}
  .card-meta {{ display:flex; flex-wrap:wrap; gap:0.35rem; margin-top:0.6rem; }}
  .chip {{ font-size:0.72rem; padding:0.15rem 0.55rem; border-radius:999px; background:#22262f; color:#b8bcc4; }}
  .chip.company {{ color:#e6e6e6; background:#262b36; }}
  .chip.site {{ color:#ffd28a; }}
  .chip.salary {{ color:#8de08d; }}
  .chip.date {{ color:#888; }}
  .empty {{ color:#888; }}
  .score {{ display:inline-block; font-size:0.78rem; font-weight:600; padding:0.15rem 0.6rem;
            border-radius:999px; margin-bottom:0.4rem; }}
  .score-high {{ background:#173a2b; color:#7be3a3; }}
  .score-mid  {{ background:#3a3417; color:#e3c97b; }}
  .score-low  {{ background:#3a1717; color:#e37b7b; }}
  .score-na   {{ background:#22262f; color:#888; }}
  .reason {{ font-size:0.78rem; color:#9aa0aa; margin:0.2rem 0 0.4rem; }}
</style>
</head>
<body>
<h1>Vacantes filtradas — DevOps / SRE</h1>
<p class="meta">Última corrida: {datetime.now(timezone.utc).isoformat(timespec="minutes")} UTC ·
  {len(df_new)} nuevas hoy · {len(df_master)} en histórico</p>

<div class="toolbar">
  <input id="q" type="text" placeholder="Buscar por título, empresa, ubicación...">
  <select id="siteFilter"><option value="">Todos los portales</option></select>
</div>

<h2>Nuevas hoy ({len(df_new)})</h2>
{_summary_stats(df_new)}
{_cards_section(df_new, "No hubo vacantes nuevas en esta corrida.")}

<h2>Histórico completo ({len(df_master)})</h2>
{_summary_stats(df_master)}
{_cards_section(df_master, "Aún no hay histórico.")}

<script>
const q = document.getElementById('q');
const siteFilter = document.getElementById('siteFilter');
const cards = Array.from(document.querySelectorAll('.card'));

const sites = [...new Set(cards.map(c => c.dataset.site).filter(Boolean))].sort();
for (const s of sites) {{
  const opt = document.createElement('option');
  opt.value = s; opt.textContent = s;
  siteFilter.appendChild(opt);
}}

function applyFilter() {{
  const term = q.value.toLowerCase().trim();
  const site = siteFilter.value;
  for (const c of cards) {{
    const matchesTerm = !term || c.dataset.search.includes(term);
    const matchesSite = !site || c.dataset.site === site;
    c.style.display = (matchesTerm && matchesSite) ? '' : 'none';
  }}
}}

q.addEventListener('input', applyFilter);
siteFilter.addEventListener('change', applyFilter);
</script>
</body>
</html>"""
    with open(INDEX_HTML, "w") as f:
        f.write(html)


# ---------------- main ----------------

def main() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    seen = load_seen()
    cv_text = load_cv()

    if not cv_text:
        print("[WARN] no se encontró CV en /app/cv.txt, el scoring quedará deshabilitado")

    hours_old = get_hours_old()
    df = scrape(hours_old)
    
    df_new = pd.DataFrame()
    if not df.empty:
        df = df.drop_duplicates(subset=["job_url"])
        df = df[df.apply(passes_filters, axis=1)].copy()
        new_mask = ~df["job_url"].isin(seen)
        df_new = df[new_mask].copy()
        df_new = score_new_jobs(df_new, cv_text)
        seen.update(df_new["job_url"].tolist())

    # ABSORBER DISCOVERY
    if os.path.exists(DISCOVERY_CSV):
        try:
            df_disc = pd.read_csv(DISCOVERY_CSV)
            disc_new_mask = ~df_disc["job_url"].isin(seen)
            df_disc_new = df_disc[disc_new_mask].copy()
            if not df_disc_new.empty:
                df_new = pd.concat([df_new if not df_new.empty else None, df_disc_new], ignore_index=True)
                seen.update(df_disc_new["job_url"].tolist())
                print(f"[INFO] Absorbidas {len(df_disc_new)} vacantes de discovery_new.csv")
            os.remove(DISCOVERY_CSV)
        except Exception as e:
            print(f"[WARN] Error procesando {DISCOVERY_CSV}: {e}")

    save_seen(seen)
    update_last_run()

    if df_new.empty:
        print("[INFO] no se obtuvieron resultados nuevos en esta corrida")
        df_master = pd.read_csv(MASTER_CSV) if os.path.exists(MASTER_CSV) else pd.DataFrame()
        render_html(pd.DataFrame(), df_master)
        return

    df_new.to_csv(NEW_CSV, index=False)

    if os.path.exists(MASTER_CSV):
        df_master_old = pd.read_csv(MASTER_CSV)
        df_master = pd.concat([df_master_old, df_new], ignore_index=True).drop_duplicates(subset=["job_url"])
    else:
        df_master = df_new

    df_master.to_csv(MASTER_CSV, index=False)
    render_html(df_new, df_master)
    notify_gotify(len(df_new))
    print(f"[OK] {len(df_new)} vacantes nuevas (Total histórico: {len(df_master)})")

if __name__ == "__main__":
    main()
