import os, re, json, sqlite3, hashlib
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from twilio.rest import Client

# ------------ CONFIG ------------
SEARCH_URL = ("https://www.rbauction.com.mx/cp/bulldozer-tractor-de-cadenas"
              "?freeText=d8%2Cd7%2Cd6&rbLocationLevelTwo=US-SOU&manufacturers=Cat")
# Lo que quieres vigilar:
KEYWORDS = [r"\bD6\b", r"\bD7\b", r"\bD8\b"]          # puedes añadir R8 si quieres
SOUTHEAST_STATES = {"FL","GA","AL","MS","LA","SC","NC","TN"}  # sureste de EEUU
DB_PATH = "seen_rbauction.sqlite"                     # para no repetir alertas
BASE = "https://www.rbauction.com.mx"
# ---------------------------------

# Twilio
load_dotenv()
TWILIO_SID   = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
WA_FROM      = os.getenv("WHATSAPP_FROM")
WA_TO        = os.getenv("WHATSAPP_TO")
twilio = Client(TWILIO_SID, TWILIO_TOKEN)

def setup_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS seen(
        k TEXT PRIMARY KEY
    )""")
    conn.commit()
    return conn

def norm(s): 
    return (s or "").strip()

def has_keywords(text: str) -> bool:
    t = text or ""
    return any(re.search(pat, t, flags=re.I) for pat in KEYWORDS)

def is_southeast(location: str) -> bool:
    if not location:
        return False
    # City, ST
    m = re.search(r",\s*([A-Z]{2})\b", location)
    if m and m.group(1).upper() in SOUTHEAST_STATES:
        return True
    # Nombres completos
    names = {
        "florida","georgia","alabama","mississippi","louisiana",
        "south carolina","north carolina","tennessee"
    }
    L = location.lower()
    return any(n in L for n in names)

def extract_next_items(html: str):
    """
    Lee el script __NEXT_DATA__ (Next.js) y busca objetos que parezcan ítems:
    diccionarios con 'title' y algún campo de URL.
    Devuelve lista de {title, link, location}.
    """
    soup = BeautifulSoup(html, "html.parser")
    s = soup.find("script", {"id": "__NEXT_DATA__"})
    items = []

    if s and s.string:
        try:
            data = json.loads(s.string)
        except Exception:
            data = None

        def walk(x):
            if isinstance(x, dict):
                # heurística de ítem:
                keys = {k.lower() for k in x.keys()}
                title = None
                url = None
                loc = None

                # intenta leer título
                for k in ("title","name","headline","productTitle","seoTitle"):
                    if k in x and isinstance(x[k], str):
                        title = x[k]; break

                # intenta leer url
                for k in ("url","urlPath","href","permalink","seoUrl","webUrl","link"):
                    if k in x and isinstance(x[k], str):
                        url = x[k]; break

                # intenta leer ubicación ciudad/estado
                for k in ("location","city","state","region","rbaLocationDisplayName"):
                    v = x.get(k)
                    if isinstance(v, dict):
                        for kk in ("displayName","name","label","shortName"):
                            if kk in v and isinstance(v[kk], str):
                                loc = v[kk]; break
                    elif isinstance(v, str):
                        loc = v
                    if loc: break

                if title and (url or url == ""):
                    link = urljoin(BASE, url)
                    items.append({
                        "title": norm(title),
                        "link":  link,
                        "location": norm(loc or "")
                    })

                # seguir recorriendo
                for v in x.values():
                    walk(v)

            elif isinstance(x, list):
                for v in x:
                    walk(v)

        if data:
            walk(data)

    # Fallback mínimo por si no hay __NEXT_DATA__
    if not items:
        for a in soup.select("a"):
            href = a.get("href") or ""
            title = a.get_text(" ", strip=True)
            if not href or not title:
                continue
            if not re.search(r"/(item|equipment|auction|lot)/|/inventory/|/cp/", href, re.I):
                continue
            link = urljoin(BASE, href)
            # intenta sacar una “City, ST” cerca
            loc = ""
            p = a.find_parent()
            if p:
                txt = p.get_text(" ", strip=True)
                m = re.search(r"\b[A-Za-z .]+,\s*[A-Z]{2}\b", txt)
                if m: loc = m.group(0)
            items.append({"title": title, "link": link, "location": loc})

    # quitar duplicados
    uniq = {}
    for it in items:
        uniq[(it["title"], it["link"])] = it
    return list(uniq.values())

def send_whatsapp(lines):
    body = "🔔 Ritchie Bros — nuevos D6/D7/D8 (Sureste):\n\n" + "\n\n".join(lines)
    twilio.messages.create(from_=WA_FROM, to=WA_TO, body=body)

def main():
    # 1) obtener HTML de la búsqueda
    r = requests.get(SEARCH_URL, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()

    # 2) extraer ítems del JSON interno
    items = extract_next_items(r.text)

    # 3) filtrar por modelo y región
    filtered = []
    for it in items:
        title = it["title"]
        link  = it["link"]
        loc   = it["location"]  # puede venir vacío si la lista no lo trae

        if not has_keywords(title):
            continue
        if loc and not is_southeast(loc):   # si hay ubicación y no es sureste, se descarta
            continue

        filtered.append(it)

    if not filtered:
        print("Sin coincidencias en esta pasada.")
        return

    # 4) avisar solo de nuevos (SQLite)
    conn = setup_db()
    cur = conn.cursor()
    new = []
    for it in filtered:
        key = hashlib.sha256(f"{it['title']}||{it['link']}".encode("utf-8")).hexdigest()
        cur.execute("SELECT 1 FROM seen WHERE k=?", (key,))
        if cur.fetchone() is None:
            cur.execute("INSERT INTO seen(k) VALUES(?)", (key,))
            new.append(it)
    conn.commit()
    conn.close()

    if new:
        lines = [f"• {n['title']}  [{n['location'] or 'Ubicación N/D'}]\n{n['link']}" for n in new[:6]]
        if len(new) > 6:
            lines.append(f"... y {len(new)-6} más")
        send_whatsapp(lines)
        print(f"WhatsApp enviado con {len(new)} novedad(es).")
    else:
        print("Sin novedades nuevas (ya vistas).")

if __name__ == "__main__":
    main()
