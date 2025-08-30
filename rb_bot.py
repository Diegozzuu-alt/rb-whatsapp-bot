# rb_bot.py
import os, re, json, sqlite3, hashlib, sys, requests
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from twilio.rest import Client
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse

# ------------------ CONFIG ------------------
# Página con los filtros EXACTOS (D6/D7/D8 + Southern US + Cat)
SEARCH_URL = (
    "https://www.rbauction.com.mx/cp/bulldozer-tractor-de-cadenas"
    "?freeText=d8%2Cd7%2Cd6&rbaLocationLevelTwo=US-SOU&manufacturers=Cat"
)

# Palabras clave a buscar en títulos (coinciden con D6/D7/D8)
KEYWORDS = [r"\bD6\b", r"\bD7\b", r"\bD8\b"]

# Estados del sureste de EUA por si la ubicación viene como 'City, ST'
SOUTHEAST_STATES = {"FL", "GA", "AL", "MS", "LA", "SC", "NC", "TN"}

# Ruta del archivo SQLite: por ENV para que en Render usemos /data/...
DB_PATH = os.getenv("DB_PATH", "seen_rbauction.sqlite")

BASE = "https://www.rbauction.com.mx"
UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    )
}
# --------------------------------------------

load_dotenv()

TWILIO_SID   = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
WA_FROM      = os.getenv("WHATSAPP_FROM", "").strip()  # ej: "whatsapp:+14155238886"
WA_TO        = os.getenv("WHATSAPP_TO", "").strip()    # ej: "whatsapp:+52XXXXXXXXXX"

# Nota: el cliente de Twilio solo se usa cuando mandas notificaciones salientes
twilio_client = Client(TWILIO_SID, TWILIO_TOKEN) if (TWILIO_SID and TWILIO_TOKEN) else None

# ------------------ UTILIDADES ------------------

def setup_db():
    # Crea el folder si DB_PATH incluye carpeta (p.ej. /data/...)
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS seen (k TEXT PRIMARY KEY)")
    conn.commit()
    return conn

def _has_keywords(text: str) -> bool:
    t = text or ""
    return any(re.search(p, t, re.I) for p in KEYWORDS)

def _is_southeast(loc: str) -> bool:
    """Devuelve True si la ubicación cae en el sureste de EUA."""
    if not loc:
        return False
    m = re.search(r",\s*([A-Z]{2})\b", loc)
    if m and m.group(1).upper() in SOUTHEAST_STATES:
        return True
    l = loc.lower()
    return any(w in l for w in [
        "florida", "georgia", "alabama", "mississippi",
        "louisiana", "south carolina", "north carolina", "tennessee"
    ])

def fetch_html():
    r = requests.get(SEARCH_URL, headers=UA, timeout=30)
    r.raise_for_status()
    return r.text

def fetch_total_via_api() -> int:
    """
    Intenta obtener el total de resultados del endpoint JSON con tus filtros.
    Si el WAF devuelve HTML, lanza excepción para usar fallback a HTML.
    """
    api_url = "https://www.rbauction.com.mx/api/advancedSearch"
    params = {
        "freeText": "d8,d7,d6",
        "manufacturers": "Cat",
        "rbaLocationLevelTwo": "US-SOU",
    }
    headers = {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": UA["User-Agent"],
        "Referer": SEARCH_URL,
        "Connection": "keep-alive",
    }
    r = requests.get(api_url, params=params, headers=headers, timeout=30)
    r.raise_for_status()

    ctype = (r.headers.get("Content-Type") or "").lower()
    if "application/json" not in ctype:
        raise ValueError("API devolvió contenido no-JSON")

    data = r.json()
    for k in ("total", "totalAmount", "totalCount", "count"):
        v = data.get(k) if isinstance(data, dict) else None
        if isinstance(v, int):
            return int(v)
    if isinstance(data, dict) and isinstance(data.get("results"), list):
        return len(data["results"])
    raise ValueError("No se encontró clave de total en la respuesta JSON")

def extract_total_from_page(html: str) -> int:
    """
    Extrae el total leyendo la página HTML con tus filtros.
    """
    m = re.search(r"Mostrando\s*\d+\s*-\s*\d+\s*de\s*(\d+)\s*resultados", html, re.I)
    if m:
        return int(m.group(1))
    m = re.search(r"Pr[oó]ximos\s*\((\d+)\)", html, re.I)
    if m:
        return int(m.group(1))
    soup = BeautifulSoup(html, "html.parser")
    links = set()
    for a in soup.select("a"):
        href = a.get("href") or ""
        if re.search(r"/(item|equipment|auction|lot)/|/inventory/|/cp/", href, re.I):
            links.add(href)
    return len(links)

def extract_items_from_nextdata(html: str):
    """
    Extrae items {title, link, location} recorriendo el JSON de __NEXT_DATA__.
    Si no los encuentra, hace un fallback sencillo sobre el HTML.
    """
    soup = BeautifulSoup(html, "html.parser")
    script = soup.find("script", {"id": "__NEXT_DATA__"})
    items = []

    if script and script.string:
        try:
            data = json.loads(script.string)
        except Exception:
            data = None

        def walk(x):
            if isinstance(x, dict):
                title = None; url = None; loc = None
                for k in ("title","name","headline","productTitle","seoTitle"):
                    if isinstance(x.get(k), str):
                        title = x[k]; break
                for k in ("url","urlPath","href","permalink","seoUrl","webUrl","link"):
                    if isinstance(x.get(k), str):
                        url = x[k]; break
                v = x.get("location")
                if isinstance(v, dict):
                    for kk in ("displayName","name","label","shortName"):
                        if isinstance(v.get(kk), str):
                            loc = v[kk]; break
                elif isinstance(v, str):
                    loc = v
                if title and url:
                    items.append({
                        "title": title.strip(),
                        "link": urljoin(BASE, url),
                        "location": (loc or "").strip()
                    })
                for v in x.values():
                    walk(v)
            elif isinstance(x, list):
                for v in x:
                    walk(v)

        if data:
            walk(data)

    if not items:
        for a in soup.select("a"):
            href = a.get("href") or ""
            title = a.get_text(" ", strip=True)
            if not href or not title:
                continue
            if not re.search(r"/(item|equipment|auction|lot)/|/inventory/|/cp/", href, re.I):
                continue
            link = urljoin(BASE, href)
            loc = ""
            parent = a.find_parent()
            if parent:
                txt = parent.get_text(" ", strip=True)
                m = re.search(r"\b[A-Za-z .]+,\s*[A-Z]{2}\b", txt)
                if m:
                    loc = m.group(0)
            items.append({"title": title, "link": link, "location": loc})

    uniq = {}
    for it in items:
        uniq[(it["title"], it["link"])] = it
    return list(uniq.values())

def check_new_items(send_whatsapp=True):
    """
    Revisa la página, filtra D6/D7/D8 y sureste, guarda vistos y
    retorna (nuevos, total_en_pagina).
    """
    html = fetch_html()
    total = extract_total_from_page(html)
    items = extract_items_from_nextdata(html)

    filtered = []
    for it in items:
        if not _has_keywords(it["title"]):
            continue
        if it["location"] and not _is_southeast(it["location"]):
            continue
        filtered.append(it)

    conn = setup_db(); cur = conn.cursor()
    nuevos = []
    for it in filtered:
        key = hashlib.sha256(f"{it['title']}|{it['link']}".encode()).hexdigest()
        cur.execute("SELECT 1 FROM seen WHERE k=?", (key,))
        if not cur.fetchone():
            cur.execute("INSERT INTO seen(k) VALUES(?)", (key,))
            nuevos.append(it)
    conn.commit(); conn.close()

    if send_whatsapp and nuevos and twilio_client and WA_FROM and WA_TO:
        lines = [f"• {n['title']} [{n['location'] or 'Ubicación N/D'}]\n{n['link']}" for n in nuevos[:6]]
        if len(nuevos) > 6:
            lines.append(f"… y {len(nuevos)-6} más")
        body = "🔔 Checar: se agregó(n) nuevo(s) D6/D7/D8 (Sureste):\n\n" + "\n\n".join(lines)
        try:
            twilio_client.messages.create(from_=WA_FROM, to=WA_TO, body=body)
        except Exception as e:
            print(f"[twilio] error enviando whatsapp: {e}", flush=True)

    return nuevos, total

# ------------------ WEB ------------------

app = Flask(__name__)

@app.get("/health")
def health():
    return "ok", 200

@app.post("/wh")
def webhook():
    """
    WhatsApp (Twilio):
    - 'cantidad' → total con filtros (API con fallback a HTML si falla)
    - 'revisar'/'check' → busca nuevos y lista un resumen
    - otro → ayuda breve
    """
    incoming = (request.values.get("Body") or "").strip().lower()
    resp = MessagingResponse()

    if incoming in ("cantidad", "conteo", "count"):
        try:
            try:
                total = fetch_total_via_api()  # 1) API
            except Exception:
                total = extract_total_from_page(fetch_html())  # 2) Fallback HTML
            resp.message(f"🔢 Resultados actuales con tus filtros (Southern US): {total}")
        except Exception as e:
            resp.message(f"⚠️ No pude obtener el conteo: {e}")
        return str(resp)

    if incoming in ("revisar", "check", "checar"):
        try:
            nuevos, total = check_new_items(send_whatsapp=False)
            if nuevos:
                lines = [f"• {n['title']} [{n['location'] or 'Ubicación N/D'}]" for n in nuevos[:5]]
                resp.message("✅ Nuevos detectados:\n" + "\n".join(lines) + f"\n\nTotal listados: {total}")
            else:
                resp.message(f"Sin novedades nuevas. Total listados: {total}")
        except Exception as e:
            resp.message(f"⚠️ Error al revisar: {e}")
        return str(resp)

    resp.message("Comandos:\n• *cantidad* → total de resultados\n• *revisar* → busca nuevos y los lista")
    return str(resp)

# ------------------ CLI local ------------------

if __name__ == "__main__":
    # Para pruebas locales (en Render se usa gunicorn)
    port = int(os.getenv("PORT", 5000))
    print(f"Listening on http://localhost:{port}/wh")
    app.run(host="0.0.0.0", port=port, debug=False)
