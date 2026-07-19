"""
scraper.py
----------
Corre según el schedule del CronJob. Scrapea vacantes con JobSpy usando los
términos de búsqueda generados desde tu CV (no una lista fija de keywords),
las puntúa contra tu CV con IA, absorbe lo que haya encontrado discovery.py,
guarda históricos, genera el dashboard y notifica por Gotify.
"""

import json
import os
import time
from datetime import datetime, timezone

import pandas as pd
import requests
from jobspy import scrape_jobs

import common

DATA_DIR = "/data"
SEEN_FILE = os.path.join(DATA_DIR, "seen.json")
MASTER_CSV = os.path.join(DATA_DIR, "master.csv")
NEW_CSV = os.path.join(DATA_DIR, "new_today.csv")
INDEX_HTML = os.path.join(DATA_DIR, "index.html")
DISCOVERY_CSV = os.path.join(DATA_DIR, "discovery_new.csv")
JOBSPY_HISTORY_FILE = os.path.join(DATA_DIR, "jobspy_history.json")

COUNTRY_INDEED = os.environ.get("COUNTRY_INDEED", "mexico")
SITES = common.get_env_list("SITES", ["indeed", "linkedin", "google", "zip_recruiter"])
RESULTS_WANTED = int(os.environ.get("RESULTS_WANTED", "40"))
IA_MAX_JOBS_TO_SCORE = int(os.environ.get("IA_MAX_JOBS_TO_SCORE", "40"))


# ---------------- dedup e historial ----------------

def load_seen() -> set:
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    return set()


def save_seen(seen: set) -> None:
    with open(SEEN_FILE, "w") as f:
        json.dump(sorted(seen), f)


def get_hours_old() -> int:
    if os.path.exists(JOBSPY_HISTORY_FILE):
        try:
            with open(JOBSPY_HISTORY_FILE) as f:
                last_run = json.load(f).get("last_run")
            if last_run:
                dt = datetime.fromisoformat(last_run)
                hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
                return max(1, int(hours) + 1)
        except Exception:
            pass
    return 24


def update_last_run() -> None:
    with open(JOBSPY_HISTORY_FILE, "w") as f:
        json.dump({"last_run": datetime.now(timezone.utc).isoformat()}, f)


# ---------------- filtro estructural (no de contenido) ----------------
# Solo datos objetivos (ubicación / remoto). Los criterios de "esto encaja
# con mi perfil o no" ya no viven aquí: los decide el match_score de la IA.

def passes_location_filter(row) -> bool:
    location = str(row.get("location", "")).lower()
    is_remote_flag = bool(row.get("is_remote", False))
    return (
        any(loc in location for loc in common.LOCATION_ALLOW)
        or any(hint in location for hint in common.REMOTE_HINTS)
        or is_remote_flag
    )


# ---------------- scraping ----------------

def scrape(search_terms: list[str], hours_old: int) -> pd.DataFrame:
    frames = []
    print(f"[INFO] Scrapeando JobSpy (últimas {hours_old}h) para: {', '.join(search_terms)}")
    for term in search_terms:
        try:
            jobs = scrape_jobs(
                site_name=SITES,
                search_term=term,
                google_search_term=f"{term} jobs near {common.LOCATION}",
                location=common.LOCATION,
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


# ---------------- scoring con IA ----------------

def score_job(cv_clean: str, row) -> tuple[int, str]:
    prompt = f"""Eres un asistente que evalúa qué tan bien encaja una vacante con el
perfil de un candidato. Responde directo, sin razonar paso a paso.

CV del candidato:
{cv_clean}

Vacante:
Título: {row.get('title', '')}
Empresa: {row.get('company', '')}
Ubicación: {row.get('location', '')}
Descripción: {str(row.get('description', '') or '')[:900]}

Responde SOLO con JSON: {{"score": <0-100>, "reason": "<una frase corta en español>"}}
"""
    result = common.ia_chat_json(prompt, attempts=2)
    if result is None:
        return -1, "error de scoring (sin respuesta de IA)"
    try:
        return int(result.get("score", -1)), str(result.get("reason", ""))
    except Exception:
        return -1, "error de scoring (respuesta malformada)"


def score_new_jobs(df_new: pd.DataFrame, cv_clean: str) -> pd.DataFrame:
    if df_new.empty or not cv_clean or not common.IA_URL:
        df_new["match_score"] = -1
        df_new["match_reason"] = "scoring deshabilitado (falta CV o IA_URL)"
        return df_new

    scores, reasons = [], []
    for i, (_, row) in enumerate(df_new.iterrows()):
        if i >= IA_MAX_JOBS_TO_SCORE:
            scores.append(-1)
            reasons.append("no evaluado (límite de corrida alcanzado)")
            continue
        s, r = score_job(cv_clean, row)
        scores.append(s)
        reasons.append(r)
        time.sleep(1)

    df_new = df_new.copy()
    df_new["match_score"] = scores
    df_new["match_reason"] = reasons
    return df_new


def passes_match_score(row) -> bool:
    score = row.get("match_score", -1)
    # -1 = no evaluado todavía (IA caída, límite alcanzado): se deja pasar
    # para no perder la vacante, y se muestra sin badge de score.
    if score is None or (isinstance(score, float) and pd.isna(score)):
        return True
    try:
        score = int(score)
    except (TypeError, ValueError):
        return True
    return score < 0 or score >= common.MIN_MATCH_SCORE


# ---------------- HTML ----------------

def _esc(val) -> str:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return ""
    return str(val).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _score_class(score) -> str:
    try:
        score = int(score)
    except (TypeError, ValueError):
        return "score-na"
    if score < 0:
        return "score-na"
    if score >= 75:
        return "score-high"
    if score >= common.MIN_MATCH_SCORE:
        return "score-mid"
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
    if df.empty:
        return ""
    by_site = df["site"].value_counts().to_dict() if "site" in df.columns else {}
    chips = "".join(f'<span class="stat-chip">{_esc(site)}: {n}</span>' for site, n in by_site.items())
    return f'<div class="stats">{chips}</div>'


def _cards_section(df: pd.DataFrame, empty_msg: str) -> str:
    if df.empty:
        return f'<p class="empty">{empty_msg}</p>'
    ordered = df.sort_values("match_score", ascending=False) if "match_score" in df.columns else df
    return '<div class="grid">' + "".join(_job_card(r) for _, r in ordered.iterrows()) + "</div>"


def render_html(df_new: pd.DataFrame, df_master: pd.DataFrame, profile: dict) -> None:
    terms_html = ", ".join(_esc(t) for t in profile.get("search_terms", []))
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
  .profile-terms {{ color:#666; font-size:0.75rem; margin-top:0.3rem; }}
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
<h1>Vacantes filtradas — perfil generado desde tu CV</h1>
<p class="meta">Última corrida: <span id="lastRunTime" data-utc="{datetime.now(timezone.utc).isoformat()}"></span> ·
  {len(df_new)} nuevas hoy · {len(df_master)} en histórico ·
  <span style="display:inline-flex; align-items:center; gap:5px;">
    Estado de IA: <span id="aiStatusDot" style="width:10px; height:10px; border-radius:50%; background:gray; display:inline-block;" title="Comprobando..."></span>
  </span>
</p>
<p class="profile-terms">Buscando: {terms_html}</p>

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

const lastRunSpan = document.getElementById('lastRunTime');
function updateTimeAgo() {{
  if (!lastRunSpan) return;
  const utcDate = new Date(lastRunSpan.dataset.utc);
  const diffMins = Math.floor((new Date() - utcDate) / 60000);
  const diffHours = Math.floor(diffMins / 60);
  let timeAgo = diffMins < 1 ? 'hace menos de 1 minuto'
    : diffMins < 60 ? `hace ${{diffMins}} min`
    : diffHours < 24 ? `hace ${{diffHours}} hr`
    : `hace ${{Math.floor(diffHours / 24)}} días`;
  lastRunSpan.textContent = `${{utcDate.toLocaleString()}} (${{timeAgo}})`;
}}
updateTimeAgo();
setInterval(updateTimeAgo, 60000);

const aiUrl = "{common.IA_URL}";
const aiStatusDot = document.getElementById('aiStatusDot');
async function pingAI() {{
  if (!aiUrl || !aiStatusDot) return;
  try {{
    await fetch(aiUrl, {{ mode: 'no-cors', cache: 'no-store' }});
    aiStatusDot.style.background = '#8de08d';
    aiStatusDot.title = "IA Alcanzable";
  }} catch (err) {{
    aiStatusDot.style.background = '#e37b7b';
    aiStatusDot.title = "IA No Alcanzable";
  }}
}}
pingAI();
setInterval(pingAI, 300000);
</script>
</body>
</html>"""
    with open(INDEX_HTML, "w") as f:
        f.write(html)


# ---------------- main ----------------

def main() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    seen = load_seen()

    profile = common.ensure_profile()
    cv_clean = common.load_cv_clean()
    if not cv_clean:
        print("[WARN] no hay CV disponible, el scoring quedará deshabilitado")

    hours_old = get_hours_old()
    df = scrape(profile.get("search_terms", []), hours_old)

    df_new = pd.DataFrame()
    if not df.empty:
        df = df.drop_duplicates(subset=["job_url"])
        df = df[df.apply(passes_location_filter, axis=1)].copy()
        new_mask = ~df["job_url"].isin(seen)
        df_new = df[new_mask].copy()
        df_new = score_new_jobs(df_new, cv_clean)
        df_new = df_new[df_new.apply(passes_match_score, axis=1)].copy()
        seen.update(df["job_url"].tolist())  # marca como vistas aunque no pasen el score, para no reevaluarlas

    # Absorber lo que encontró discovery.py
    if os.path.exists(DISCOVERY_CSV):
        try:
            df_disc = pd.read_csv(DISCOVERY_CSV)
            df_disc_new = df_disc[~df_disc["job_url"].isin(seen)].copy()
            if not df_disc_new.empty:
                df_new = pd.concat([df_new, df_disc_new], ignore_index=True) if not df_new.empty else df_disc_new
                seen.update(df_disc_new["job_url"].tolist())
                print(f"[INFO] Absorbidas {len(df_disc_new)} vacantes de discovery_new.csv")
            os.remove(DISCOVERY_CSV)
        except Exception as e:
            print(f"[WARN] Error procesando {DISCOVERY_CSV}: {e}")

    save_seen(seen)
    update_last_run()

    df_master = pd.read_csv(MASTER_CSV) if os.path.exists(MASTER_CSV) else pd.DataFrame()

    if df_new.empty:
        print("[INFO] no se obtuvieron resultados nuevos en esta corrida")
        render_html(pd.DataFrame(), df_master, profile)
        return

    df_new.to_csv(NEW_CSV, index=False)
    df_master = pd.concat([df_master, df_new], ignore_index=True).drop_duplicates(subset=["job_url"]) \
        if not df_master.empty else df_new
    df_master.to_csv(MASTER_CSV, index=False)

    render_html(df_new, df_master, profile)
    common.notify_gotify(len(df_new))
    print(f"[OK] {len(df_new)} vacantes nuevas (Total histórico: {len(df_master)})")


if __name__ == "__main__":
    main()
