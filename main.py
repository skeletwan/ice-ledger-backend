import os, json, base64, time, re, sqlite3, hashlib, secrets, hmac
from datetime import datetime, timedelta, timezone
from io import BytesIO
from fastapi import FastAPI, UploadFile, File, Header, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse, Response
from urllib.parse import urlparse, quote_plus
from pathlib import Path
from PIL import Image
import httpx

XAI_API_KEY = os.environ.get("XAI_API_KEY", "")
APP_SECRET = os.environ.get("APP_SECRET", "")
OPERATOR_EMAIL = (os.environ.get("OPERATOR_EMAIL", "iceledger@outlook.com") or "iceledger@outlook.com").lower()
MODEL = os.environ.get("XAI_MODEL", "grok-4-1-fast-non-reasoning")
DAILY_CAP = int(os.environ.get("DAILY_CAP", "80"))
FREE_SCANS = int(os.environ.get("FREE_SCANS", "10"))
PLUS_SCANS = int(os.environ.get("PLUS_SCANS", "200"))
FREE_BOOK = int(os.environ.get("FREE_BOOK", "8"))
PLUS_BOOK = int(os.environ.get("PLUS_BOOK", "30"))
STRIPE_PAY_LINK = os.environ.get("STRIPE_PAY_LINK", "")
STRIPE_RIP_LINK = os.environ.get("STRIPE_RIP_LINK", "")
RIP_SCANS = int(os.environ.get("RIP_SCANS", "20"))
RIP_PRICE = os.environ.get("RIP_PRICE", "2.99 CAD")
def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name, default) or default).strip().strip('"').strip("'")

MAIL_TO = _env("MAIL_TO", "iceledger@outlook.com")
MAIL_FROM = _env("MAIL_FROM", "Ice Ledger <noreply@contact.iceledgerz.com>")
RESEND_API_KEY = _env("RESEND_API_KEY")
SMTP_HOST = _env("SMTP_HOST")
SMTP_PORT = int(_env("SMTP_PORT", "587") or "587")
SMTP_USER = _env("SMTP_USER")
SMTP_PASS = _env("SMTP_PASS")
APP_URL = _env("APP_URL")
MAIL_LAST_ERROR = ""

def _from_address(raw: str) -> str:
    raw = (raw or "").strip()
    if "<" in raw and ">" in raw:
        return raw[raw.find("<") + 1:raw.find(">")].strip()
    return raw

def send_mail(subject: str, body: str, to: str | None = None) -> bool:
    global MAIL_LAST_ERROR
    MAIL_LAST_ERROR = ""
    to = (to or MAIL_TO or "").strip().lower()
    if not to or "@" not in to:
        MAIL_LAST_ERROR = "no recipient"
        return False
    sender = MAIL_FROM or f"Ice Ledger <{MAIL_TO}>"
    addr = _from_address(sender)
    if RESEND_API_KEY:
        try:
            r = httpx.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
                json={"from": sender, "to": [to], "subject": subject, "text": body},
                timeout=20,
            )
            if r.status_code < 300:
                return True
            MAIL_LAST_ERROR = f"Resend {r.status_code}: {(r.text or '')[:400]}"
            print("MAIL_FAIL", MAIL_LAST_ERROR)
        except Exception as e:
            MAIL_LAST_ERROR = f"Resend error: {e}"
            print("MAIL_FAIL", MAIL_LAST_ERROR)
    if SMTP_HOST and SMTP_USER and SMTP_PASS:
        try:
            import smtplib
            from email.mime.text import MIMEText
            msg = MIMEText(body)
            msg["Subject"] = subject
            msg["From"] = sender
            msg["To"] = to
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as s:
                s.starttls()
                s.login(SMTP_USER, SMTP_PASS)
                s.sendmail(addr or sender, [to], msg.as_string())
            return True
        except Exception as e:
            MAIL_LAST_ERROR = f"SMTP error: {e}"
            print("MAIL_FAIL", MAIL_LAST_ERROR)
            return False
    if not MAIL_LAST_ERROR:
        MAIL_LAST_ERROR = "no RESEND_API_KEY and no SMTP settings"
    return False

app = FastAPI(title="Ice Ledger Identify")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_hits = {}

PROMPT = """Identify this trading card. Ice Ledger only catalogs HOCKEY right now.
Hockey = NHL, AHL, CHL (OHL/WHL/QMJHL), IIHF, Team Canada/USA, PWHL, junior/international hockey.
If the card is baseball, basketball, football, soccer, Pokemon, TCG, entertainment, or anything else: set hockey false and sport to that category. Do not pretend it is hockey.
Return ONLY JSON, no markdown.
You may get FRONT and sometimes BACK. Back is source of truth for year, set name, card number, copyright line.
Slab: read the grading label first (grader, grade, cert), then the card through the case.

Upper Deck hockey rules (2015–2026 especially):
- Young Guns = insert "Young Guns" (not a parallel). Canvas Young Guns = insert "Young Guns Canvas".
- Exclusives, High Gloss, Clear Cut, Outburst, Traxx are parallels or separate inserts — never label a plain YG as those.
- "C" or Young Guns badge on silver UD Series 1/2 rookies is usually Young Guns, not SP Authentic.
- Copy set name from the back: Series 1, Series 2, Extended, SP Authentic, SP Game Used, The Cup, Stature, Premier, Allure, Synergy, Metal Universe, Chronology, Trilogy, O-Pee-Chee, Parkhurst, Choice, Fleer Ultra, Skybox Impact.
- Vintage 1990s: never return only "Ultra" or only "Impact". Set must include the brand: Fleer Ultra, Skybox Impact, Score, Pinnacle, Donruss, Leaf, Topps, OPC, Stadium Club, Be A Player. Rookie / RC on those cards is insert "Rookie", not the set.
- Parallel examples: Silver Foil, Gold /100, Exclusives /100, High Gloss /10, Clear Cut, Outburst Gold, Rainbow, Black /1. If no /n and no foil name, parallel is null or Base.
- Do not invent a numbered parallel because the photo is shiny.
- 1990s Pinnacle / Score / Donruss / Leaf: Starquest, Artist's Proofs, Rink Collection, Ice Breakers. Starquest color versions are parallels — Green, Red, Blue, Gold, Purple, Black. If the card face or foil is clearly green, parallel is "Green" (not Base). Same for other named colors.
- Set name Starquest (Pinnacle) is the set, not an insert, when the front says STARQUEST. Player still from the photo (e.g. Eric Lindros).
- Upper Deck Choice (1998–99 Choice, 1999–00 Choice, etc.): if the card says Choice Reserve, set is "Choice" (or the full year + Choice from the back) and parallel/insert is "Reserve" — do not call it Series 1 or a generic Upper Deck base. Choice Preview, Choice Reserve Mini, Choice StarQuest-style names stay as printed. Joe Thornton Choice Reserve is player Thornton, set Choice, parallel or insert Reserve.

Foil / color (required look):
- foil_color = dominant color of the foil or card stock if it is clearly not a normal white/cream base: Green, Red, Blue, Gold, Purple, Black, Silver, Bronze, Orange. "A bit shiny" is not a color. A green Starquest face is Green.
- foil_text = exact words you can read in the foil stamp or colored plate (CHOICE, RESERVE, STARQUEST, EXCLUSIVES, etc.). Copy them even if stylized.
- If foil_color is a named color and parallel is empty or Base, set parallel to that color.
- If foil_text includes RESERVE with Choice, parallel or insert is Reserve.

If a field is not readable, use null. Never invent a rare parallel.
{
  "hockey": boolean,
  "player": string|null,
  "sport": string|null,
  "year": string|null,
  "set": string|null,
  "number": string|null,
  "parallel": string|null,
  "insert": string|null,
  "team": string|null,
  "grader": "Raw"|"PSA"|"BGS"|"SGC"|"CGC"|null,
  "grade": string|null,
  "cert": string|null,
  "foil_color": string|null,
  "foil_text": string|null,
  "confidence": {
    "player": number,
    "year": number,
    "set": number,
    "number": number,
    "parallel": number,
    "insert": number,
    "grader": number,
    "grade": number,
    "cert": number
  },
  "needs_review": boolean,
  "notes": string
}
confidence is 0 to 1. Set needs_review true if any important field is under 0.7 or parallel is uncertain.
"""

ROOT = Path(__file__).parent
DB_PATH = Path(os.environ.get("DB_PATH", str(ROOT / "ice.db")))

def check_secret(secret: str | None):
    if APP_SECRET and secret != APP_SECRET:
        raise HTTPException(401, "bad secret")

def allow_user_or_secret(secret: str | None, x_token: str | None = None):
    if user_from_token(x_token):
        return
    check_secret(secret)

@app.get("/", response_class=HTMLResponse)
def home():
    page = ROOT / "app.html"
    if page.exists():
        return FileResponse(page)
    return HTMLResponse("<p>app.html missing</p>")

def legal_file():
    page = ROOT / "legal.html"
    if page.exists():
        return FileResponse(page)
    return HTMLResponse("<p>legal.html missing</p>")

@app.get("/legal")
def legal_home():
    return legal_file()

@app.get("/terms")
def legal_terms():
    return legal_file()

@app.get("/privacy")
def legal_privacy():
    return legal_file()

@app.get("/aup")
def legal_aup():
    return legal_file()

def check_cap():
    day = time.strftime("%Y-%m-%d")
    n = _hits.get(day, 0)
    if n >= DAILY_CAP:
        raise HTTPException(429, f"daily cap {DAILY_CAP} reached")
    _hits[day] = n + 1

def shrink(data: bytes) -> bytes:
    img = Image.open(BytesIO(data))
    img = img.convert("RGB")
    img.thumbnail((1280, 1280))
    out = BytesIO()
    img.save(out, format="JPEG", quality=80)
    return out.getvalue()

@app.get("/health")
def health():
    return {
        "ok": True,
        "model": MODEL,
        "key_set": bool(XAI_API_KEY),
        "resend_key": bool(RESEND_API_KEY),
        "mail_from": MAIL_FROM,
        "mail_to": MAIL_TO,
        "smtp_set": bool(SMTP_HOST and SMTP_USER and SMTP_PASS),
    }

async def _jpeg_part(up: UploadFile, label: str):
    raw = await up.read()
    if len(raw) > 12_000_000:
        raise HTTPException(400, f"{label} image too large")
    try:
        jpeg = shrink(raw)
    except Exception:
        raise HTTPException(400, f"{label} is not a readable image")
    b64 = base64.b64encode(jpeg).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "high"}}

@app.post("/identify")
async def identify(
    file: UploadFile = File(...),
    back: UploadFile | None = File(default=None),
    x_app_secret: str | None = Header(default=None),
    x_token: str | None = Header(default=None),
    secret: str | None = Form(default=None),
):
    allow_user_or_secret(x_app_secret or secret, x_token)
    if not XAI_API_KEY:
        raise HTTPException(500, "XAI_API_KEY not set on server")
    check_cap()
    require_scan(None, x_token)
    content = [
        {"type": "text", "text": "FRONT of card:"},
        await _jpeg_part(file, "front"),
    ]
    if back and back.filename:
        content += [
            {"type": "text", "text": "BACK of card:"},
            await _jpeg_part(back, "back"),
        ]
    content.append({"type": "text", "text": PROMPT})
    payload = {
        "model": MODEL,
        "temperature": 0,
        "messages": [{"role": "user", "content": content}],
    }
    async with httpx.AsyncClient(timeout=90) as client:
        r = await client.post(
            "https://api.x.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {XAI_API_KEY}", "Content-Type": "application/json"},
            json=payload,
        )
    if r.status_code >= 400:
        raise HTTPException(502, f"xAI error {r.status_code}: {r.text[:400]}")
    text = r.json()["choices"][0]["message"]["content"]
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text[: text.rfind("```")]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise HTTPException(502, "model did not return JSON")
    data["model"] = MODEL
    sport = str(data.get("sport") or "").strip().lower()
    notes = str(data.get("notes") or "").lower()
    hockey_words = ("hockey", "nhl", "ahl", "ohl", "whl", "qmjhl", "chl", "iihf", "pwhl")
    other_words = ("pokemon", "pokémon", "baseball", "mlb", "basketball", "nba", "football", "nfl", "soccer", "fifa", "mtg", "magic", "yugioh", "yu-gi-oh")
    hockey = data.get("hockey")
    if hockey is None:
        hockey = any(w in sport for w in hockey_words) or (not sport and not any(w in notes for w in other_words))
    if any(w in sport for w in other_words):
        hockey = False
    data["hockey"] = bool(hockey)
    foil = re.sub(r"[^a-z ]", "", str(data.get("foil_color") or "").strip().lower())
    foil_text = str(data.get("foil_text") or "")
    par = str(data.get("parallel") or "").strip()
    ins = str(data.get("insert") or "").strip()
    st = str(data.get("set") or "").strip()
    blob = " ".join([st, par, ins, foil_text, str(data.get("player") or "")]).lower()
    color_map = {
        "green": "Green", "red": "Red", "blue": "Blue", "gold": "Gold",
        "purple": "Purple", "black": "Black", "silver": "Silver",
        "bronze": "Bronze", "orange": "Orange", "teal": "Teal",
    }
    named = color_map.get(foil.split()[0] if foil else "")
    if named and (not par or par.lower() in ("base", "null", "none", "raw")):
        data["parallel"] = named
        par = named
    set_fix = {
        "ultra": "Fleer Ultra",
        "impact": "Skybox Impact",
    }
    st_l = st.lower().strip()
    if st_l in set_fix:
        data["set"] = set_fix[st_l]
        st = data["set"]
    if re.search(r"\brc\b|rookie", " ".join([st, par, ins, str(data.get("player") or "")]).lower()):
        if not ins or ins.lower() in ("base", "null", "none"):
            if "rookie" not in st_l:
                data["insert"] = "Rookie"
    if "choice" in blob and "reserve" in blob:
        if "choice" not in st.lower():
            data["set"] = (st + " Choice").strip() if st else "Choice"
        if "reserve" not in (par + " " + ins).lower():
            data["parallel"] = ((par + " Reserve").strip() if par and par.lower() not in ("base",) else "Reserve")
    if not data["hockey"]:
        data["needs_review"] = True
        data["blocked"] = True
        label = data.get("sport") or "not hockey"
        data["notes"] = (data.get("notes") or "") + f" Ice Ledger is hockey-only for now ({label})."
    return data


COMP_MODEL = os.environ.get("COMP_MODEL", "grok-4-1-fast-non-reasoning")
COMP_PROMPT = """Search recent SOLD / completed hockey card sales for this exact card (not asking prices).
Prefer public sold results and Fanatics Collect auction history. Do not scrape eBay. Do not use 130point.
Card: {card}
Return ONLY JSON, no markdown:
{{
  "suggested_cad": number|null,
  "suggested_usd": number|null,
  "raw_cad": number|null,
  "psa8_cad": number|null,
  "psa9_cad": number|null,
  "psa10_cad": number|null,
  "bgs95_cad": number|null,
  "sgc10_cad": number|null,
  "low": number|null,
  "high": number|null,
  "currency": "CAD"|"USD"|null,
  "sample_count": number,
  "confidence": number,
  "needs_review": boolean,
  "summary": string,
  "sources": [string]
}}
Search separately for RAW solds, PSA 8, PSA 9, PSA 10, BGS 9.5, and SGC 10 of this same player/set/number/parallel.
Fill each *_cad field you can. suggested_cad is the price for THIS copy's grader/grade. If this copy is Raw, suggested_cad MUST equal raw_cad.
Only use sold sale prices (money). Never use the card number, year, print run, or cert as a price.
If you cannot find a sold price for a grade, leave that field null. Do not copy one grade into another.
Convert USD to CAD at 1.35.
"""

def _extract_response_text(body: dict) -> str:
    if isinstance(body.get("output_text"), str) and body["output_text"]:
        return body["output_text"]
    chunks = []
    for item in body.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            for c in item.get("content") or []:
                if isinstance(c, dict) and c.get("text"):
                    chunks.append(c["text"])
        if item.get("type") == "output_text" and item.get("text"):
            chunks.append(item["text"])
    if chunks:
        return "\n".join(chunks)
    try:
        return body["choices"][0]["message"]["content"] or ""
    except Exception:
        return json.dumps(body)[:2000]

def _parse_json_blob(text: str) -> dict:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text[: text.rfind("```")]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end+1])
        raise

@app.post("/comp")
async def comp(
    payload: dict,
    x_app_secret: str | None = Header(default=None),
    x_token: str | None = Header(default=None),
):
    allow_user_or_secret(x_app_secret or payload.get("secret"), x_token)
    if not XAI_API_KEY:
        raise HTTPException(500, "XAI_API_KEY not set on server")
    check_cap()
    card = {k: payload.get(k) for k in ("player","year","set","number","parallel","insert","team","grader","grade","cert")}
    label = ", ".join(f"{k}={v}" for k,v in card.items() if v)
    headers = {"Authorization": f"Bearer {XAI_API_KEY}", "Content-Type": "application/json"}
    prompt = COMP_PROMPT.format(card=label)
    text = ""
    used = COMP_MODEL
    err = None
    async with httpx.AsyncClient(timeout=40) as client:
        r = await client.post(
            "https://api.x.ai/v1/responses",
            headers=headers,
            json={"model": COMP_MODEL, "tools": [{"type": "web_search"}], "input": prompt},
        )
        if r.status_code < 400:
            text = _extract_response_text(r.json()).strip()
        else:
            err = f"{r.status_code}: {r.text[:180]}"
    data = {}
    try:
        data = _parse_json_blob(text) if text else {}
    except json.JSONDecodeError:
        data = {"summary": (text or "")[:280], "needs_review": True}
    if not isinstance(data, dict):
        data = {"summary": str(data)[:280], "needs_review": True}
    def _as_price(v):
        try:
            n = float(v)
        except (TypeError, ValueError):
            return None
        if n < 1 or n > 20000:
            return None
        return n

    def _looks_like_card_no(n):
        raw = str(card.get("number") or "").strip().lstrip("#")
        if not raw:
            return False
        try:
            return abs(float(n) - float(raw)) < 0.001
        except ValueError:
            return str(int(n)) == raw if float(n).is_integer() else False

    grades = data.get("grades") or data.get("book") or {}
    if isinstance(grades, dict):
        alias = {
            "raw": "raw_cad", "raw_cad": "raw_cad",
            "psa8": "psa8_cad", "psa 8": "psa8_cad",
            "psa9": "psa9_cad", "psa 9": "psa9_cad",
            "psa10": "psa10_cad", "psa 10": "psa10_cad",
            "bgs95": "bgs95_cad", "bgs 9.5": "bgs95_cad",
            "sgc10": "sgc10_cad", "sgc 10": "sgc10_cad",
        }
        for k, v in grades.items():
            dest = alias.get(str(k).lower().strip())
            if dest and data.get(dest) is None:
                data[dest] = v

    text_l = text or ""
    for rx, dest in (
        (r"PSA\s*10[^0-9]{0,12}(?:CAD|USD|C\$|US\$|\$)\s*([0-9]{1,5}(?:\.[0-9]{1,2})?)", "psa10_cad"),
        (r"PSA\s*9(?:\.0)?[^0-9.]{0,12}(?:CAD|USD|C\$|US\$|\$)\s*([0-9]{1,5}(?:\.[0-9]{1,2})?)", "psa9_cad"),
        (r"PSA\s*8(?:\.0)?[^0-9.]{0,12}(?:CAD|USD|C\$|US\$|\$)\s*([0-9]{1,5}(?:\.[0-9]{1,2})?)", "psa8_cad"),
        (r"BGS\s*9\.5[^0-9]{0,12}(?:CAD|USD|C\$|US\$|\$)\s*([0-9]{1,5}(?:\.[0-9]{1,2})?)", "bgs95_cad"),
        (r"SGC\s*10[^0-9]{0,12}(?:CAD|USD|C\$|US\$|\$)\s*([0-9]{1,5}(?:\.[0-9]{1,2})?)", "sgc10_cad"),
        (r"(?:raw|ungraded)[^0-9]{0,16}(?:CAD|USD|C\$|US\$|\$)\s*([0-9]{1,5}(?:\.[0-9]{1,2})?)", "raw_cad"),
    ):
        if data.get(dest) is None:
            m = re.search(rx, text_l, flags=re.I)
            if m:
                data[dest] = m.group(1)

    for key in ("suggested_cad", "suggested_usd", "raw_cad", "psa8_cad", "psa9_cad", "psa10_cad", "bgs95_cad", "sgc10_cad", "low", "high"):
        n = _as_price(data.get(key))
        if n is None:
            data[key] = None
        elif key in ("suggested_cad", "suggested_usd") and _looks_like_card_no(n):
            data[key] = None
        else:
            data[key] = n
    nums = []
    for m in re.findall(r"(?:CAD|USD|C\$|US\$|\$)\s*([0-9]{1,5}(?:\.[0-9]{1,2})?)", text or "", flags=re.I):
        v = _as_price(m)
        if v is not None and not _looks_like_card_no(v):
            nums.append(v)
    if data.get("suggested_cad") is None and data.get("suggested_usd") is None and nums:
        mid = sorted(nums)[len(nums)//2]
        data["suggested_cad"] = round(mid, 2)
        data["low"] = data.get("low") or min(nums)
        data["high"] = data.get("high") or max(nums)
        data["needs_review"] = True
        data["summary"] = data.get("summary") or f"Estimated from sold text around ${mid:.0f}"
    data["model"] = used
    data["card"] = card
    if err and not data.get("suggested_cad") and not data.get("suggested_usd"):
        data["error"] = err
        data["needs_review"] = True
        data["summary"] = data.get("summary") or "Could not read solds automatically."
    return data


PHOTO_PROMPT = """Find current eBay listing photos of this exact hockey card.
Card: {card}
Only use eBay image hosts (i.ebayimg.com). No Google or Bing URLs.
Return ONLY JSON:
{{ "images": [ {{ "url": "https://i.ebayimg.com/images/g/..../s-l1600.jpg", "label": "ebay" }} ] }}
Give 6-10 card photos. Skip logos and avatars.
"""

def _urls_from_obj(obj, found):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("url", "image_url", "src") and isinstance(v, str) and v.startswith("http"):
                found.append(v)
            else:
                _urls_from_obj(v, found)
    elif isinstance(obj, list):
        for v in obj:
            _urls_from_obj(v, found)
    elif isinstance(obj, str) and obj.startswith("http") and any(obj.lower().endswith(x) or x in obj.lower() for x in (".jpg", ".jpeg", ".png", ".webp", "ebayimg", "i.ebay")):
        found.append(obj)

@app.post("/photos")
async def photos(
    payload: dict,
    x_app_secret: str | None = Header(default=None),
    x_token: str | None = Header(default=None),
):
    allow_user_or_secret(x_app_secret or payload.get("secret"), x_token)
    if not XAI_API_KEY:
        raise HTTPException(500, "XAI_API_KEY not set on server")
    check_cap()
    card = {k: payload.get(k) for k in ("player","year","set","number","parallel","insert","team","grader","grade","cert")}
    label = ", ".join(f"{k}={v}" for k,v in card.items() if v)
    headers = {"Authorization": f"Bearer {XAI_API_KEY}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(
            "https://api.x.ai/v1/responses",
            headers=headers,
            json={
                "model": COMP_MODEL,
                "tools": [{"type": "web_search", "enable_image_search": True, "allowed_domains": ["ebay.com","ebay.ca","ebayimg.com","i.ebayimg.com"]}],
                "input": PHOTO_PROMPT.format(card=label),
            },
        )
    if r.status_code >= 400:
        raise HTTPException(502, f"xAI photo error {r.status_code}: {r.text[:300]}")
    body = r.json()
    text = _extract_response_text(body)
    images = []
    try:
        parsed = _parse_json_blob(text)
        for item in parsed.get("images") or []:
            if isinstance(item, str) and item.startswith("http"):
                images.append({"url": item, "label": ""})
            elif isinstance(item, dict) and str(item.get("url","")).startswith("http"):
                images.append({"url": item["url"], "label": item.get("label") or ""})
    except Exception:
        pass
    extra = []
    _urls_from_obj(body, extra)
    _urls_from_obj(text, extra)
    for u in extra:
        if u.startswith("http") and not any(x["url"] == u for x in images):
            images.append({"url": u, "label": ""})
    clean = []
    for im in images:
        u = im.get("url") or ""
        low = u.lower()
        if not u.startswith("http"):
            continue
        if any(bad in low for bad in ("google.", "gstatic", "bing.", "logo", "favicon", "sprite", "1x1", "doubleclick")):
            continue
        if "ebayimg" not in low and "ebaystatic" not in low and "ebay.com" not in low:
            continue
        if "ebayimg.com" in low and "/s-l" not in low and "images/g/" not in low:
            if "i.ebayimg.com" in low and "s-l1600" not in low:
                u = re.sub(r"/s-l\d+", "/s-l1600", u)
        im = {"url": u, "label": im.get("label") or "eBay"}
        if not any(x["url"] == u for x in clean):
            clean.append(im)
        if len(clean) >= 12:
            break
    return {"images": clean, "query": label}

@app.get("/img-proxy")
async def img_proxy(url: str = ""):
    host = (urlparse(url).hostname or "").lower()
    if not url.startswith("https://") or not any(h in host for h in ("ebayimg.com", "ebaystatic.com")):
        raise HTTPException(400, "only ebay images")
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
    if r.status_code >= 400:
        raise HTTPException(404, "image not found")
    ctype = r.headers.get("content-type", "image/jpeg")
    if not ctype.startswith("image/"):
        raise HTTPException(400, "not an image")
    return Response(content=r.content, media_type=ctype)


def db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con = db()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS users (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      email TEXT UNIQUE NOT NULL,
      pw TEXT NOT NULL,
      created TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS sessions (
      token TEXT PRIMARY KEY,
      user_id INTEGER NOT NULL,
      created TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS cards (
      id TEXT PRIMARY KEY,
      user_id INTEGER NOT NULL,
      data TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS usage (
      user_id INTEGER NOT NULL,
      month TEXT NOT NULL,
      n INTEGER NOT NULL,
      PRIMARY KEY (user_id, month)
    );
    CREATE TABLE IF NOT EXISTS book_usage (
      user_id INTEGER NOT NULL,
      month TEXT NOT NULL,
      n INTEGER NOT NULL,
      PRIMARY KEY (user_id, month)
    );
    """)
    try:
        con.execute("ALTER TABLE users ADD COLUMN plan TEXT DEFAULT 'free'")
    except sqlite3.OperationalError:
        pass
    try:
        con.execute("ALTER TABLE users ADD COLUMN slug TEXT")
    except sqlite3.OperationalError:
        pass
    for col, spec in (("display", "TEXT"), ("hue", "TEXT"), ("bio", "TEXT"), ("avatar", "TEXT"), ("cropx", "TEXT"), ("cropy", "TEXT"), ("cropz", "TEXT"), ("avatar_hidden", "INTEGER NOT NULL DEFAULT 0"), ("credits", "INTEGER NOT NULL DEFAULT 0"), ("credit_month", "TEXT"), ("credit_until", "TEXT"), ("plus_until", "TEXT"), ("cycle_start", "TEXT"), ("suspended", "INTEGER NOT NULL DEFAULT 0"), ("socials", "TEXT")):
        try:
            con.execute(f"ALTER TABLE users ADD COLUMN {col} {spec}")
        except sqlite3.OperationalError:
            pass
    con.execute("""
    CREATE TABLE IF NOT EXISTS comments (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      slug TEXT NOT NULL,
      card_id TEXT NOT NULL,
      user_id INTEGER NOT NULL,
      name TEXT NOT NULL,
      body TEXT NOT NULL,
      created TEXT NOT NULL
    )
    """)
    try:
        con.execute("ALTER TABLE comments ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        con.execute("ALTER TABLE comments ADD COLUMN parent_id INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    con.execute("""
    CREATE TABLE IF NOT EXISTS likes (
      slug TEXT NOT NULL,
      card_id TEXT NOT NULL,
      user_id INTEGER NOT NULL,
      created TEXT NOT NULL,
      PRIMARY KEY (slug, card_id, user_id)
    )
    """)
    con.execute("""
    CREATE TABLE IF NOT EXISTS binder_likes (
      slug TEXT NOT NULL,
      user_id INTEGER NOT NULL,
      created TEXT NOT NULL,
      PRIMARY KEY (slug, user_id)
    )
    """)
    con.execute("""
    CREATE TABLE IF NOT EXISTS follows (
      follower INTEGER NOT NULL,
      slug TEXT NOT NULL,
      created TEXT NOT NULL,
      PRIMARY KEY (follower, slug)
    )
    """)
    con.execute("""
    CREATE TABLE IF NOT EXISTS reports (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      slug TEXT NOT NULL,
      reporter INTEGER,
      reason TEXT,
      created TEXT NOT NULL
    )
    """)
    con.execute("""
    CREATE TABLE IF NOT EXISTS rip_codes (
      code TEXT PRIMARY KEY,
      scans INTEGER NOT NULL,
      max_uses INTEGER NOT NULL,
      used INTEGER NOT NULL DEFAULT 0,
      note TEXT,
      created TEXT NOT NULL
    )
    """)
    try:
        con.execute("ALTER TABLE rip_codes ADD COLUMN expires TEXT")
    except sqlite3.OperationalError:
        pass
    con.execute("""
    CREATE TABLE IF NOT EXISTS rip_redemptions (
      code TEXT NOT NULL,
      user_id INTEGER NOT NULL,
      created TEXT NOT NULL,
      PRIMARY KEY (code, user_id)
    )
    """)
    con.execute("""
    CREATE TABLE IF NOT EXISTS banner_posts (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER NOT NULL,
      kind TEXT NOT NULL,
      title TEXT NOT NULL,
      body TEXT,
      url TEXT,
      color TEXT,
      created TEXT NOT NULL
    )
    """)
    con.execute("""
    CREATE TABLE IF NOT EXISTS notes (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER NOT NULL,
      slug TEXT,
      body TEXT NOT NULL,
      created TEXT NOT NULL,
      read INTEGER NOT NULL DEFAULT 0
    )
    """)
    try:
        con.execute("ALTER TABLE notes ADD COLUMN card_id TEXT")
    except sqlite3.OperationalError:
        pass
    con.execute("""
    CREATE TABLE IF NOT EXISTS resets (
      email TEXT NOT NULL,
      token TEXT NOT NULL,
      created INTEGER NOT NULL
    )
    """)
    con.commit()
    con.close()

def ensure_slug(uid: int) -> str:
    con = db()
    row = con.execute("SELECT email, slug FROM users WHERE id=?", (uid,)).fetchone()
    if not row:
        con.close()
        raise HTTPException(404, "no user")
    if row["slug"]:
        s = row["slug"]
        con.close()
        return s
    base = re.sub(r"[^a-z0-9]+", "", (row["email"] or "collector").split("@")[0].lower())[:12] or "ice"
    s = f"{base}{uid}"
    con.execute("UPDATE users SET slug=? WHERE id=?", (s, uid))
    con.commit()
    con.close()
    return s

def is_grail(c):
    if not isinstance(c, dict):
        return False
    blob = " ".join(str(c.get(k) or "") for k in ("parallel", "insert", "set", "number")).lower()
    if re.search(r"\b1\s*/\s*1\b|\b1 of 1\b|one of one|superfractor|printing plate", blob):
        return True
    if re.search(r"(?<![0-9])/1(?![0-9])", blob):
        return True
    return False

def card_market(c):
    if not isinstance(c, dict):
        return None
    for k in ("sysComp", "sys_comp", "rawComp"):
        v = c.get(k)
        try:
            n = float(v)
        except (TypeError, ValueError):
            continue
        if 1 <= n <= 20000:
            return n
    return None

def public_card(raw: dict) -> dict:
    c = dict(raw or {})
    return {
        "id": c.get("id"),
        "player": c.get("player"),
        "year": c.get("year"),
        "set": c.get("set"),
        "number": c.get("number"),
        "insert": c.get("insert"),
        "parallel": c.get("parallel"),
        "team": c.get("team"),
        "grader": c.get("grader"),
        "grade": c.get("grade"),
        "photo": "",
        "has_photo": bool((c.get("photo") or c.get("scan") or "") and len(str(c.get("photo") or c.get("scan") or "")) > 80),
        "comp": c.get("comp"),
        "book": c.get("book") or {},
        "hist": (c.get("hist") or [])[-60:],
        "added": c.get("added"),
        "grail": is_grail(c),
    }

init_db()

def hash_pw(pw: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 120000)
    return salt + "$" + dk.hex()

def check_pw(pw: str, stored: str) -> bool:
    try:
        salt, hx = stored.split("$", 1)
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 120000)
    return hmac.compare_digest(dk.hex(), hx)

def user_from_token(token: str | None):
    if not token:
        return None
    con = db()
    row = con.execute("SELECT user_id FROM sessions WHERE token=?", (token,)).fetchone()
    con.close()
    return row["user_id"] if row else None

def require_user(request: Request = None, x_token: str | None = None):
    token = x_token or (request.headers.get("x-token") if request else None)
    uid = user_from_token(token)
    if not uid:
        raise HTTPException(401, "sign in")
    con = db()
    row = con.execute("SELECT suspended FROM users WHERE id=?", (uid,)).fetchone()
    con.close()
    if row and int(row["suspended"] or 0):
        raise HTTPException(403, "account suspended")
    return uid

def operator_emails():
    return [e.strip().lower() for e in OPERATOR_EMAIL.split(",") if e.strip()]

def email_of(uid: int) -> str:
    con = db()
    row = con.execute("SELECT email FROM users WHERE id=?", (uid,)).fetchone()
    con.close()
    return ((row["email"] if row else "") or "").lower()

def is_operator(uid: int) -> bool:
    return email_of(uid) in operator_emails()

def require_operator(request: Request = None, x_token: str | None = None):
    uid = require_user(request, x_token)
    if not is_operator(uid):
        raise HTTPException(403, "operator only")
    return uid

def add_note(con, user_id, slug, body, card_id=""):
    if not user_id:
        return
    con.execute(
        "INSERT INTO notes(user_id,slug,body,created,read,card_id) VALUES(?,?,?,?,0,?)",
        (user_id, slug or "", body[:180], time.strftime("%Y-%m-%dT%H:%M:%SZ"), card_id or ""),
    )

def notify_activity(con, owner_id, slug, body, card_id="", actor_id=None):
    seen = set()
    targets = [owner_id]
    if slug:
        for f in con.execute("SELECT follower FROM follows WHERE slug=?", (slug,)).fetchall():
            targets.append(f["follower"])
    for tid in targets:
        if not tid or tid in seen:
            continue
        seen.add(tid)
        add_note(con, tid, slug, body, card_id)

def display_of(con, uid: int) -> str:
    row = con.execute("SELECT display FROM users WHERE id=?", (uid,)).fetchone()
    return ((row["display"] if row else None) or "Collector")[:24]

def person_of(con, uid: int) -> dict | None:
    row = con.execute(
        "SELECT slug, display, avatar, IFNULL(avatar_hidden,0) AS avatar_hidden FROM users WHERE id=?",
        (uid,),
    ).fetchone()
    if not row or not row["slug"]:
        return None
    return {
        "slug": row["slug"],
        "display": (row["display"] or "Collector")[:24],
        "has_avatar": bool(row["avatar"]) and not int(row["avatar_hidden"] or 0),
    }

def now_utc():
    return datetime.now(timezone.utc)

def parse_ts(s):
    if not s:
        return None
    raw = str(s).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except Exception:
        try:
            dt = datetime.strptime(raw[:10], "%Y-%m-%d")
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt

def month_key(uid: int | None = None):
    if uid is None:
        return now_utc().strftime("%Y-%m-%d")
    start, _ = ensure_cycle(uid)
    return start.strftime("%Y-%m-%d")

def ensure_cycle(uid: int):
    con = db()
    row = con.execute(
        "SELECT created, cycle_start, plus_until, plan FROM users WHERE id=?",
        (uid,),
    ).fetchone()
    now = now_utc()
    plus_until = parse_ts(row["plus_until"] if row else None)
    start = parse_ts(row["cycle_start"] if row else None) or parse_ts(row["created"] if row else None) or now
    while start + timedelta(days=30) <= now:
        start = start + timedelta(days=30)
    end = start + timedelta(days=30)
    con.execute("UPDATE users SET cycle_start=? WHERE id=?", (start.strftime("%Y-%m-%dT%H:%M:%SZ"), uid))
    if plus_until and plus_until <= now and ((row["plan"] if row else "") == "plus"):
        con.execute("UPDATE users SET plan='free' WHERE id=?", (uid,))
    con.commit()
    con.close()
    return start, end

def plan_of(uid: int) -> str:
    con = db()
    row = con.execute("SELECT plan, plus_until FROM users WHERE id=?", (uid,)).fetchone()
    now = now_utc()
    until = parse_ts(row["plus_until"] if row else None)
    if until and until > now:
        con.close()
        return "plus"
    p = (row["plan"] if row else "free") or "free"
    if p == "plus" and not until:
        until = now + timedelta(days=30)
        con.execute(
            "UPDATE users SET plus_until=?, cycle_start=? WHERE id=?",
            (until.strftime("%Y-%m-%dT%H:%M:%SZ"), now.strftime("%Y-%m-%dT%H:%M:%SZ"), uid),
        )
        con.commit()
        con.close()
        return "plus"
    if p == "plus" and until and until <= now:
        con.execute("UPDATE users SET plan='free' WHERE id=?", (uid,))
        con.commit()
    con.close()
    return "free"

def bonus_of(uid: int) -> int:
    con = db()
    row = con.execute("SELECT credits, credit_until, credit_month FROM users WHERE id=?", (uid,)).fetchone()
    con.close()
    try:
        if not row:
            return 0
        until = parse_ts(row["credit_until"] if "credit_until" in row.keys() else None)
        if until:
            if until <= now_utc():
                return 0
            return max(0, int((row["credits"] or 0) or 0))
        cm = row["credit_month"] or ""
        if cm and len(cm) == 7 and cm != now_utc().strftime("%Y-%m"):
            return 0
        return max(0, int((row["credits"] or 0) or 0))
    except Exception:
        return 0

def add_credits(uid: int, n: int):
    until = now_utc() + timedelta(days=30)
    mark = until.strftime("%Y-%m-%dT%H:%M:%SZ")
    con = db()
    row = con.execute("SELECT credits, credit_until FROM users WHERE id=?", (uid,)).fetchone()
    cur = 0
    old = parse_ts(row["credit_until"] if row else None)
    if row and old and old > now_utc():
        cur = int(row["credits"] or 0)
    con.execute(
        "UPDATE users SET credits=?, credit_until=?, credit_month=? WHERE id=?",
        (cur + int(n), mark, until.strftime("%Y-%m-%d"), uid),
    )
    con.commit()
    con.close()

def usage_of(uid: int):
    start, end = ensure_cycle(uid)
    m = start.strftime("%Y-%m-%d")
    plan = plan_of(uid)
    cap = PLUS_SCANS if plan == "plus" else FREE_SCANS
    bcap = PLUS_BOOK if plan == "plus" else FREE_BOOK
    con = db()
    row = con.execute("SELECT n FROM usage WHERE user_id=? AND month=?", (uid, m)).fetchone()
    brow = con.execute("SELECT n FROM book_usage WHERE user_id=? AND month=?", (uid, m)).fetchone()
    prow = con.execute("SELECT plus_until FROM users WHERE id=?", (uid,)).fetchone()
    con.close()
    used = row["n"] if row else 0
    bused = brow["n"] if brow else 0
    bonus = bonus_of(uid)
    monthly_left = max(0, cap - used)
    reset_at = (parse_ts(prow["plus_until"] if prow else None) if plan == "plus" else end) or end
    return {
        "used": used, "cap": cap, "bonus": bonus,
        "left": monthly_left + bonus,
        "book_used": bused, "book_cap": bcap, "book_left": max(0, bcap - bused),
        "plan": plan, "month": m,
        "reset_at": reset_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rip_scans": RIP_SCANS, "rip_price": RIP_PRICE,
    }

def bump_usage(uid: int):
    m = month_key(uid)
    con = db()
    row = con.execute("SELECT n FROM usage WHERE user_id=? AND month=?", (uid, m)).fetchone()
    used = row["n"] if row else 0
    plan = plan_of(uid)
    cap = PLUS_SCANS if plan == "plus" else FREE_SCANS
    if used < cap:
        if row:
            con.execute("UPDATE usage SET n=n+1 WHERE user_id=? AND month=?", (uid, m))
        else:
            con.execute("INSERT INTO usage(user_id,month,n) VALUES(?,?,1)", (uid, m))
    else:
        if bonus_of(uid) > 0:
            con.execute("UPDATE users SET credits=MAX(0, IFNULL(credits,0)-1) WHERE id=?", (uid,))
    con.commit()
    con.close()

def bump_book(uid: int):
    m = month_key(uid)
    con = db()
    row = con.execute("SELECT n FROM book_usage WHERE user_id=? AND month=?", (uid, m)).fetchone()
    if row:
        con.execute("UPDATE book_usage SET n=n+1 WHERE user_id=? AND month=?", (uid, m))
    else:
        con.execute("INSERT INTO book_usage(user_id,month,n) VALUES(?,?,1)", (uid, m))
    con.commit()
    con.close()

def require_scan(request: Request, x_token: str | None = None):
    uid = require_user(request, x_token)
    u = usage_of(uid)
    if u["left"] <= 0:
        raise HTTPException(402, "scan cap reached — buy Rip Night or upgrade")
    bump_usage(uid)
    return uid, usage_of(uid)

@app.post("/signup")
async def signup(payload: dict):
    email = (payload.get("email") or "").strip().lower()
    pw = (payload.get("password") or "").strip()
    if "@" not in email or len(pw) < 6:
        raise HTTPException(400, "email and password (6+ chars)")
    con = db()
    try:
        con.execute(
            "INSERT INTO users(email,pw,created) VALUES(?,?,?)",
            (email, hash_pw(pw), time.strftime("%Y-%m-%dT%H:%M:%SZ")),
        )
        con.commit()
    except sqlite3.IntegrityError:
        con.close()
        raise HTTPException(409, "email already used")
    uid = con.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()["id"]
    token = secrets.token_urlsafe(24)
    con.execute("INSERT INTO sessions(token,user_id,created) VALUES(?,?,?)",
                (token, uid, time.strftime("%Y-%m-%dT%H:%M:%SZ")))
    con.commit()
    con.close()
    return {"token": token, "email": email}

@app.post("/login")
async def login(payload: dict):
    email = (payload.get("email") or "").strip().lower()
    pw = (payload.get("password") or "").strip()
    con = db()
    row = con.execute("SELECT id,pw,suspended FROM users WHERE email=?", (email,)).fetchone()
    if not row:
        con.close()
        raise HTTPException(401, "no account with that email")
    if not check_pw(pw, row["pw"]):
        con.close()
        raise HTTPException(401, "wrong password")
    if int(row["suspended"] or 0):
        con.close()
        raise HTTPException(403, "account suspended")
    token = secrets.token_urlsafe(24)
    con.execute("INSERT INTO sessions(token,user_id,created) VALUES(?,?,?)",
                (token, row["id"], time.strftime("%Y-%m-%dT%H:%M:%SZ")))
    con.commit()
    con.close()
    return {"token": token, "email": email}

@app.get("/usage")
async def usage(request: Request, x_token: str | None = Header(default=None)):
    uid = require_user(request, x_token)
    return usage_of(uid)

@app.post("/book-refresh")
async def book_refresh(payload: dict, request: Request, x_token: str | None = Header(default=None)):
    uid = require_user(request, x_token)
    u = usage_of(uid)
    if u["book_left"] <= 0:
        raise HTTPException(402, "book refresh cap reached — upgrade")
    bump_book(uid)
    return await comp(payload, payload.get("secret"), x_token)

@app.get("/checkout")
async def checkout(request: Request, x_token: str | None = Header(default=None)):
    require_user(request, x_token)
    item = (request.query_params.get("item") if request else "") or ""
    if item == "rip":
        return {
            "url": STRIPE_RIP_LINK or None,
            "price": RIP_PRICE,
            "scans": RIP_SCANS,
            "name": "Rip Night",
            "note": None if STRIPE_RIP_LINK else "Set STRIPE_RIP_LINK on Railway, or redeem a code.",
        }
    if STRIPE_PAY_LINK:
        return {"url": STRIPE_PAY_LINK, "price": "8 CAD / month"}
    return {"url": None, "price": "8 CAD / month", "note": "Set STRIPE_PAY_LINK on Railway when the Stripe Payment Link is live."}

@app.post("/plus")
async def plus(payload: dict, request: Request, x_token: str | None = Header(default=None)):
    """Flip plan to plus. Operator account only until Stripe is live."""
    uid = require_operator(request, x_token or payload.get("token"))
    if APP_SECRET and payload.get("secret") != APP_SECRET:
        raise HTTPException(401, "app secret does not match")
    until = now_utc() + timedelta(days=30)
    mark = until.strftime("%Y-%m-%dT%H:%M:%SZ")
    start = now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")
    con = db()
    con.execute("UPDATE users SET plan='plus', plus_until=?, cycle_start=? WHERE id=?", (mark, start, uid))
    con.commit()
    con.close()
    return usage_of(uid)


@app.post("/rip/redeem")
async def rip_redeem(payload: dict, request: Request, x_token: str | None = Header(default=None)):
    uid = require_user(request, x_token)
    code = re.sub(r"[^A-Za-z0-9\-]", "", (payload.get("code") or "")).upper()
    if len(code) < 4:
        raise HTTPException(400, "enter a code")
    con = db()
    row = con.execute("SELECT code,scans,max_uses,used,created,expires FROM rip_codes WHERE code=?", (code,)).fetchone()
    if not row:
        con.close()
        raise HTTPException(404, "code not found")
    exp = parse_ts(row["expires"] if "expires" in row.keys() else None) or (
        parse_ts(row["created"]) + timedelta(days=30) if parse_ts(row["created"]) else None
    )
    if exp and exp <= now_utc():
        con.close()
        raise HTTPException(400, "Rip Night code expired")
    if int(row["used"]) >= int(row["max_uses"]):
        con.close()
        raise HTTPException(400, "code already used up")
    taken = con.execute("SELECT code FROM rip_redemptions WHERE code=? AND user_id=?", (code, uid)).fetchone()
    if taken:
        con.close()
        raise HTTPException(400, "you already used this code")
    con.execute("UPDATE rip_codes SET used=used+1 WHERE code=?", (code,))
    con.execute(
        "INSERT INTO rip_redemptions(code,user_id,created) VALUES(?,?,?)",
        (code, uid, time.strftime("%Y-%m-%dT%H:%M:%SZ")),
    )
    con.commit()
    con.close()
    add_credits(uid, int(row["scans"]))
    return usage_of(uid) | {"added": int(row["scans"]), "code": code}


@app.post("/admin/rip-code")
async def admin_rip_code(payload: dict, request: Request, x_token: str | None = Header(default=None)):
    require_operator(request, x_token)
    scans = max(1, min(500, int(payload.get("scans") or 10)))
    uses = max(1, min(200, int(payload.get("max_uses") or 1)))
    note = (payload.get("note") or "")[:80]
    code = (payload.get("code") or "").strip().upper() or ("RIP-" + secrets.token_hex(3).upper())
    code = re.sub(r"[^A-Z0-9\-]", "", code)
    con = db()
    try:
        created = now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")
        expires = (now_utc() + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        con.execute(
            "INSERT INTO rip_codes(code,scans,max_uses,used,note,created,expires) VALUES(?,?,?,?,?,?,?)",
            (code, scans, uses, 0, note, created, expires),
        )
        con.commit()
    except sqlite3.IntegrityError:
        con.close()
        raise HTTPException(409, "that code already exists")
    con.close()
    return {"code": code, "scans": scans, "max_uses": uses, "note": note, "expires": expires}


@app.get("/admin/rip-codes")
async def admin_rip_codes(request: Request, x_token: str | None = Header(default=None)):
    require_operator(request, x_token)
    con = db()
    rows = con.execute("SELECT code,scans,max_uses,used,note,created,expires FROM rip_codes ORDER BY created DESC LIMIT 40").fetchall()
    con.close()
    return {"codes": [dict(r) for r in rows]}


@app.post("/admin/grant-rip")
async def admin_grant_rip(payload: dict, request: Request, x_token: str | None = Header(default=None)):
    require_operator(request, x_token)
    email = (payload.get("email") or "").strip().lower()
    scans = max(1, min(500, int(payload.get("scans") or RIP_SCANS)))
    con = db()
    row = con.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
    if not row:
        con.close()
        raise HTTPException(404, "no account with that email")
    uid = row["id"]
    con.close()
    add_credits(uid, scans)
    send_mail("Ice Ledger Rip Night", f"Granted {scans} extra IDs to {email}")
    return {"ok": True, "email": email, "scans": scans}

@app.post("/reset")
async def reset_request(payload: dict):
    email = (payload.get("email") or "").strip().lower()
    if "@" not in email:
        raise HTTPException(400, "enter the account email")
    con = db()
    row = con.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
    if row:
        token = secrets.token_urlsafe(8).lower()
        con.execute("DELETE FROM resets WHERE email=?", (email,))
        con.execute("INSERT INTO resets(email,token,created) VALUES(?,?,?)", (email, token, int(time.time())))
        con.commit()
        sent = send_mail(
            "Ice Ledger password reset",
            f"Your Ice Ledger reset code is: {token}\n\nIt expires in 30 minutes. If you didn't ask for this, ignore the email.",
            to=email,
        )
        if not sent:
            print("RESET_MAIL_FAIL", email, MAIL_LAST_ERROR)
        found, mailed = True, sent
    else:
        found, mailed = False, False
        print("RESET_NO_USER", email)
    con.close()
    out = {"ok": True}
    if APP_SECRET and payload.get("secret") == APP_SECRET:
        out.update({"found": found, "mailed": mailed, "error": MAIL_LAST_ERROR, "from": MAIL_FROM})
    return out


def _mail_test_body(to: str):
    ok = send_mail("Ice Ledger mail test", "If you got this, mail is working on Ice Ledger.", to=to)
    return {"ok": ok, "to": to, "from": MAIL_FROM, "resend_key": bool(RESEND_API_KEY), "error": MAIL_LAST_ERROR}


@app.get("/mail-test")
async def mail_test_get(secret: str = "", email: str = ""):
    if not APP_SECRET or secret != APP_SECRET:
        raise HTTPException(401, "app secret does not match")
    return _mail_test_body((email or MAIL_TO).strip().lower())


@app.post("/mail-test")
async def mail_test(payload: dict):
    if not APP_SECRET or payload.get("secret") != APP_SECRET:
        raise HTTPException(401, "app secret does not match")
    to = (payload.get("email") or MAIL_TO or "").strip().lower()
    return _mail_test_body(to)

@app.post("/reset-confirm")
async def reset_confirm(payload: dict):
    email = (payload.get("email") or "").strip().lower()
    code = (payload.get("code") or payload.get("token") or "").strip().lower()
    pw = payload.get("password") or ""
    if "@" not in email or len(pw) < 6 or len(code) < 4:
        raise HTTPException(400, "email, reset code, and a new password (6+)")
    con = db()
    row = con.execute("SELECT token,created FROM resets WHERE email=?", (email,)).fetchone()
    if not row or row["token"] != code or int(time.time()) - int(row["created"]) > 1800:
        con.close()
        raise HTTPException(400, "code is wrong or expired")
    user = con.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
    if not user:
        con.close()
        raise HTTPException(404, "no account with that email")
    con.execute("UPDATE users SET pw=? WHERE id=?", (hash_pw(pw), user["id"]))
    con.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))
    con.execute("DELETE FROM resets WHERE email=?", (email,))
    token = secrets.token_urlsafe(24)
    con.execute(
        "INSERT INTO sessions(token,user_id,created) VALUES(?,?,?)",
        (token, user["id"], time.strftime("%Y-%m-%dT%H:%M:%SZ")),
    )
    con.commit()
    con.close()
    return {"token": token, "email": email}

@app.get("/cards")
async def list_cards(request: Request, x_token: str | None = Header(default=None)):
    uid = require_user(request, x_token)
    con = db()
    rows = con.execute("SELECT data FROM cards WHERE user_id=?", (uid,)).fetchall()
    con.close()
    out = []
    for r in rows:
        try:
            raw = json.loads(r["data"])
        except Exception:
            continue
        pc = public_card(raw)
        pc["cost"] = raw.get("cost")
        pc["notes"] = raw.get("notes")
        out.append(pc)
    return {"cards": out}

@app.post("/cards")
async def upsert_card(payload: dict, request: Request, x_token: str | None = Header(default=None)):
    uid = require_user(request, x_token)
    card = payload.get("card") or payload
    cid = card.get("id") or ("c" + str(int(time.time()*1000)))
    card["id"] = cid
    # do not store giant data-urls if huge
    for k in ("scan", "photo"):
        v = card.get(k)
        if isinstance(v, str) and len(v) > 1800000:
            card[k] = v[:1800000]
    con = db()
    existed = con.execute("SELECT id FROM cards WHERE id=? AND user_id=?", (cid, uid)).fetchone()
    con.execute(
        "INSERT OR REPLACE INTO cards(id,user_id,data) VALUES(?,?,?)",
        (cid, uid, json.dumps(card)),
    )
    if not existed:
        owner = con.execute("SELECT slug, display FROM users WHERE id=?", (uid,)).fetchone()
        slug = (owner["slug"] if owner else "") or ""
        who = (owner["display"] if owner else None) or "Collector"
        player = (card.get("player") or "a card")
        created = time.strftime("%Y-%m-%dT%H:%M:%SZ")
        grail = is_grail(card)
        body = f"GRAIL: {who} added {player}" if grail else f"{who} added {player}"
        fans = con.execute("SELECT follower FROM follows WHERE slug=?", (slug,)).fetchall() if slug else []
        for f in fans:
            con.execute(
                "INSERT INTO notes(user_id,slug,body,created,read,card_id) VALUES(?,?,?,?,0,?)",
                (f["follower"], slug, body, created, cid),
            )
    con.commit()
    con.close()
    return {"ok": True, "id": cid}

@app.delete("/cards/{cid}")
async def delete_card(cid: str, request: Request, x_token: str | None = Header(default=None)):
    uid = require_user(request, x_token)
    con = db()
    con.execute("DELETE FROM cards WHERE id=? AND user_id=?", (cid, uid))
    con.commit()
    con.close()
    return {"ok": True}

@app.get("/binders")
async def list_binders():
    con = db()
    users = con.execute(
        "SELECT id, slug, display, hue, bio, avatar, avatar_hidden, cropx, cropy, cropz, IFNULL(suspended,0) AS suspended FROM users WHERE slug IS NOT NULL AND slug != ''"
    ).fetchall()
    out = []
    for u in users:
        if int(u["suspended"] or 0):
            continue
        rows = con.execute("SELECT data FROM cards WHERE user_id=?", (u["id"],)).fetchall()
        if not rows:
            continue
        cards = []
        for r in rows:
            try:
                cards.append(json.loads(r["data"]))
            except Exception:
                pass
        book = 0.0
        teams, players = [], []
        for c in cards:
            mv = None
            for k in ("sysComp", "sys_comp", "rawComp"):
                if isinstance(c.get(k), (int, float)) and 1 <= float(c[k]) <= 20000:
                    mv = float(c[k])
                    break
            if mv is not None:
                book += mv
            if c.get("team"):
                teams.append(c["team"])
            if c.get("player"):
                players.append(c["player"])
        top = max(set(teams), key=teams.count) if teams else ""
        out.append({
            "slug": u["slug"],
            "display": u["display"] or "Collector",
            "hue": u["hue"] or "#8fd4ee",
            "bio": u["bio"] or "",
            "has_avatar": bool(u["avatar"]) and not int(u["avatar_hidden"] or 0),
            "cropx": u["cropx"] or "50",
            "cropy": u["cropy"] or "50",
            "cropz": u["cropz"] or "100",
            "count": len(cards),
            "grails": sum(1 for c in cards if is_grail(c)),
            "book": round(book, 2),
            "team": top,
            "players": " ".join(players).lower(),
        })
        likes = con.execute("SELECT COUNT(*) AS n FROM binder_likes WHERE slug=?", (u["slug"],)).fetchone()["n"]
        out[-1]["likes"] = likes
    con.close()
    return {"binders": out}

@app.get("/feed")
async def recent_feed():
    con = db()
    users = con.execute(
        "SELECT id, slug, display, IFNULL(suspended,0) AS suspended FROM users WHERE slug IS NOT NULL AND slug != ''"
    ).fetchall()
    items = []
    for u in users:
        if int(u["suspended"] or 0):
            continue
        rows = con.execute("SELECT data FROM cards WHERE user_id=?", (u["id"],)).fetchall()
        for r in rows:
            try:
                raw = json.loads(r["data"])
            except Exception:
                continue
            c = public_card(raw)
            if not c.get("id"):
                continue
            items.append({
                "slug": u["slug"],
                "display": u["display"] or "Collector",
                "id": c.get("id"),
                "player": c.get("player"),
                "year": c.get("year"),
                "set": c.get("set"),
                "number": c.get("number"),
                "insert": c.get("insert"),
                "parallel": c.get("parallel"),
                "team": c.get("team"),
                "grader": c.get("grader"),
                "grade": c.get("grade"),
                "photo": c.get("photo"),
                "comp": c.get("comp"),
                "added": c.get("added") or "",
                "grail": bool(c.get("grail")),
            })
    con.close()
    items.sort(key=lambda x: str(x.get("added") or ""), reverse=True)
    return {"cards": items[:80]}

@app.post("/u/{slug}/like")
async def like_binder(slug: str, request: Request, x_token: str | None = Header(default=None)):
    uid = require_user(request, x_token)
    slug = re.sub(r"[^a-z0-9]", "", (slug or "").lower())
    con = db()
    if not con.execute("SELECT id FROM users WHERE slug=?", (slug,)).fetchone():
        con.close()
        raise HTTPException(404, "binder not found")
    row = con.execute("SELECT user_id FROM binder_likes WHERE slug=? AND user_id=?", (slug, uid)).fetchone()
    if row:
        con.execute("DELETE FROM binder_likes WHERE slug=? AND user_id=?", (slug, uid))
        liked = False
    else:
        con.execute(
            "INSERT INTO binder_likes(slug,user_id,created) VALUES(?,?,?)",
            (slug, uid, time.strftime("%Y-%m-%dT%H:%M:%SZ")),
        )
        liked = True
        owner = con.execute("SELECT id FROM users WHERE slug=?", (slug,)).fetchone()
        if owner:
            notify_activity(con, owner["id"], slug, display_of(con, uid)+" liked the binder", "", uid)
    con.commit()
    n = con.execute("SELECT COUNT(*) AS n FROM binder_likes WHERE slug=?", (slug,)).fetchone()["n"]
    con.close()
    return {"liked": liked, "likes": n}

@app.post("/follow/{slug}")
async def follow_binder(slug: str, request: Request, x_token: str | None = Header(default=None)):
    uid = require_user(request, x_token)
    slug = re.sub(r"[^a-z0-9]", "", (slug or "").lower())
    con = db()
    owner = con.execute("SELECT id FROM users WHERE slug=?", (slug,)).fetchone()
    if not owner:
        con.close()
        raise HTTPException(404, "binder not found")
    row = con.execute("SELECT slug FROM follows WHERE follower=? AND slug=?", (uid, slug)).fetchone()
    if row:
        con.execute("DELETE FROM follows WHERE follower=? AND slug=?", (uid, slug))
        con.commit()
        con.close()
        return {"following": False}
    con.execute(
        "INSERT INTO follows(follower,slug,created) VALUES(?,?,?)",
        (uid, slug, time.strftime("%Y-%m-%dT%H:%M:%SZ")),
    )
    con.commit()
    con.close()
    return {"following": True}

@app.get("/u/{slug}/cards/{cid}/photo")
async def card_photo(slug: str, cid: str):
    slug = re.sub(r"[^a-z0-9]", "", (slug or "").lower())
    con = db()
    u = con.execute("SELECT id, IFNULL(suspended,0) AS suspended FROM users WHERE slug=?", (slug,)).fetchone()
    if not u or int(u["suspended"] or 0):
        con.close()
        raise HTTPException(404, "no photo")
    row = con.execute("SELECT data FROM cards WHERE user_id=? AND id=?", (u["id"], cid)).fetchone()
    con.close()
    if not row:
        raise HTTPException(404, "no photo")
    try:
        raw = json.loads(row["data"])
    except Exception:
        raise HTTPException(404, "no photo")
    blob = raw.get("photo") or raw.get("scan") or ""
    if not isinstance(blob, str) or len(blob) < 80:
        raise HTTPException(404, "no photo")
    kind = "image/jpeg"
    b64 = blob
    if blob.startswith("data:"):
        head, b64 = blob.split(",", 1) if "," in blob else (blob, "")
        if "png" in head:
            kind = "image/png"
        elif "webp" in head:
            kind = "image/webp"
    b64 = re.sub(r"\s+", "", b64)
    try:
        data = base64.b64decode(b64)
    except Exception:
        raise HTTPException(404, "no photo")
    if len(data) < 32:
        raise HTTPException(404, "no photo")
    return Response(data, media_type=kind, headers={"Cache-Control": "public, max-age=120"})

@app.get("/u/{slug}/avatar")
async def binder_avatar(slug: str):
    slug = re.sub(r"[^a-z0-9]", "", (slug or "").lower())
    con = db()
    u = con.execute("SELECT avatar, avatar_hidden FROM users WHERE slug=?", (slug,)).fetchone()
    con.close()
    raw = (u["avatar"] if u else "") or ""
    if not raw or int((u["avatar_hidden"] if u else 0) or 0):
        raise HTTPException(404, "no photo")
    kind = "image/jpeg"
    b64 = raw
    if raw.startswith("data:"):
        head, b64 = raw.split(",", 1) if "," in raw else (raw, "")
        if "png" in head:
            kind = "image/png"
        elif "webp" in head:
            kind = "image/webp"
    b64 = re.sub(r"\s+", "", b64)
    try:
        data = base64.b64decode(b64)
    except Exception:
        raise HTTPException(404, "no photo")
    if len(data) < 32:
        raise HTTPException(404, "no photo")
    return Response(data, media_type=kind, headers={"Cache-Control": "public, max-age=60"})


@app.post("/report/{slug}")
async def report_binder(slug: str, payload: dict, request: Request, x_token: str | None = Header(default=None)):
    uid = require_user(request, x_token)
    slug = re.sub(r"[^a-z0-9]", "", (slug or "").lower())
    reason = re.sub(r"\s+", " ", (payload.get("reason") or "").strip())[:180]
    if len(reason) < 3:
        raise HTTPException(400, "pick a reason")
    con = db()
    if not con.execute("SELECT id FROM users WHERE slug=?", (slug,)).fetchone():
        con.close()
        raise HTTPException(404, "binder not found")
    already = con.execute(
        "SELECT id FROM reports WHERE reporter=? AND slug=? AND IFNULL(reason,'') NOT LIKE 'comment:%'",
        (uid, slug),
    ).fetchone()
    if already:
        n = con.execute(
            "SELECT COUNT(DISTINCT reporter) AS n FROM reports WHERE slug=? AND IFNULL(reason,'') NOT LIKE 'comment:%'",
            (slug,),
        ).fetchone()["n"]
        con.close()
        return {"ok": True, "reports": n, "already": True}
    con.execute(
        "INSERT INTO reports(slug,reporter,reason,created) VALUES(?,?,?,?)",
        (slug, uid, reason, time.strftime("%Y-%m-%dT%H:%M:%SZ")),
    )
    n = con.execute(
        "SELECT COUNT(DISTINCT reporter) AS n FROM reports WHERE slug=? AND IFNULL(reason,'') NOT LIKE 'comment:%'",
        (slug,),
    ).fetchone()["n"]
    hidden = False
    if n >= 3:
        con.execute("UPDATE users SET avatar_hidden=1 WHERE slug=?", (slug,))
        hidden = True
    who = email_of(uid)
    con.commit()
    con.close()
    send_mail(
        "Ice Ledger photo report",
        f"Profile photo report\nBinder: {slug}\nReporter: {who} (id {uid})\nReason: {reason}\nUnique reports: {n}\n(3 unique reports hide the photo.)",
    )
    if hidden:
        send_mail(
            "Ice Ledger photo removed",
            f"Profile photo HIDDEN after 3 unique reports.\nBinder: {slug}\nLast reason: {reason}\nRestore it from Owner tools → Removed if this was junk reporting.",
        )
    return {"ok": True, "reports": n, "already": False, "removed": hidden}

@app.post("/admin/clear-avatar")
async def admin_clear(payload: dict):
    if APP_SECRET and payload.get("secret") != APP_SECRET:
        raise HTTPException(401, "app secret does not match")
    slug = re.sub(r"[^a-z0-9]", "", (payload.get("slug") or "").lower())
    con = db()
    con.execute("UPDATE users SET avatar='' WHERE slug=?", (slug,))
    con.commit()
    con.close()
    return {"ok": True}

@app.get("/following")
async def my_follows(request: Request, x_token: str | None = Header(default=None)):
    uid = require_user(request, x_token)
    con = db()
    rows = con.execute("SELECT slug FROM follows WHERE follower=?", (uid,)).fetchall()
    con.close()
    return {"slugs": [r["slug"] for r in rows]}

@app.get("/notes")
async def list_notes(request: Request, x_token: str | None = Header(default=None)):
    uid = require_user(request, x_token)
    con = db()
    rows = con.execute(
        "SELECT id,slug,body,created,read,card_id FROM notes WHERE user_id=? ORDER BY id DESC LIMIT 60",
        (uid,),
    ).fetchall()
    unread = con.execute("SELECT COUNT(*) AS n FROM notes WHERE user_id=? AND read=0", (uid,)).fetchone()["n"]
    con.close()
    return {"notes": [dict(r) for r in rows], "unread": unread}

@app.post("/notes/read")
async def read_notes(payload: dict | None = None, request: Request = None, x_token: str | None = Header(default=None)):
    uid = require_user(request, x_token)
    ids = [int(i) for i in ((payload or {}).get("ids") or []) if str(i).isdigit() or isinstance(i, int)]
    con = db()
    if ids:
        q = ",".join("?" * len(ids))
        con.execute(f"UPDATE notes SET read=1 WHERE user_id=? AND id IN ({q})", [uid, *ids])
    else:
        con.execute("UPDATE notes SET read=1 WHERE user_id=?", (uid,))
    con.commit()
    con.close()
    return {"ok": True}

@app.post("/u/{slug}/cards/{cid}/like")
async def toggle_like(slug: str, cid: str, request: Request, x_token: str | None = Header(default=None)):
    uid = require_user(request, x_token)
    con = db()
    owner = con.execute("SELECT id FROM users WHERE slug=?", (slug,)).fetchone()
    if not owner:
        con.close()
        raise HTTPException(404, "binder not found")
    row = con.execute(
        "SELECT user_id FROM likes WHERE slug=? AND card_id=? AND user_id=?",
        (slug, cid, uid),
    ).fetchone()
    if row:
        con.execute("DELETE FROM likes WHERE slug=? AND card_id=? AND user_id=?", (slug, cid, uid))
        liked = False
    else:
        con.execute(
            "INSERT INTO likes(slug,card_id,user_id,created) VALUES(?,?,?,?)",
            (slug, cid, uid, time.strftime("%Y-%m-%dT%H:%M:%SZ")),
        )
        liked = True
        notify_activity(con, owner["id"], slug, display_of(con, uid)+" liked a card", cid, uid)
    con.commit()
    n = con.execute("SELECT COUNT(*) AS n FROM likes WHERE slug=? AND card_id=?", (slug, cid)).fetchone()["n"]
    con.close()
    return {"liked": liked, "likes": n}

@app.get("/u/{slug}/cards/{cid}/likes")
async def card_likes(slug: str, cid: str, request: Request, x_token: str | None = Header(default=None)):
    tok = x_token or (request.headers.get("x-token") if request else None)
    uid = user_from_token(tok)
    con = db()
    n = con.execute("SELECT COUNT(*) AS n FROM likes WHERE slug=? AND card_id=?", (slug, cid)).fetchone()["n"]
    mine = False
    if uid:
        mine = bool(con.execute(
            "SELECT 1 FROM likes WHERE slug=? AND card_id=? AND user_id=?",
            (slug, cid, uid),
        ).fetchone())
    people = []
    rows = con.execute("SELECT user_id FROM likes WHERE slug=? AND card_id=? ORDER BY created DESC LIMIT 40", (slug, cid)).fetchall()
    for r in rows:
        p = person_of(con, r["user_id"])
        if p:
            people.append(p)
    con.close()
    return {"likes": n, "liked": mine, "people": people}

@app.get("/u/{slug}/likers")
async def binder_likers(slug: str):
    slug = re.sub(r"[^a-z0-9]", "", (slug or "").lower())
    con = db()
    rows = con.execute("SELECT user_id FROM binder_likes WHERE slug=? ORDER BY created DESC LIMIT 80", (slug,)).fetchall()
    people = []
    for r in rows:
        p = person_of(con, r["user_id"])
        if p:
            people.append(p)
    con.close()
    return {"people": people}

@app.get("/me/community")
async def my_community(request: Request, x_token: str | None = Header(default=None)):
    uid = require_user(request, x_token)
    slug = ensure_slug(uid)
    con = db()
    fol = con.execute("SELECT follower FROM follows WHERE slug=? ORDER BY created DESC LIMIT 80", (slug,)).fetchall()
    followers = []
    for r in fol:
        p = person_of(con, r["follower"])
        if p:
            followers.append(p)
    blink = con.execute("SELECT user_id FROM binder_likes WHERE slug=? ORDER BY created DESC LIMIT 80", (slug,)).fetchall()
    binder = []
    for r in blink:
        p = person_of(con, r["user_id"])
        if p:
            binder.append(p)
    cards = []
    liked = con.execute(
        "SELECT card_id, user_id FROM likes WHERE slug=? ORDER BY created DESC LIMIT 80",
        (slug,),
    ).fetchall()
    by_card = {}
    for r in liked:
        by_card.setdefault(r["card_id"], []).append(r["user_id"])
    for cid, uids in list(by_card.items())[:20]:
        row = con.execute("SELECT data FROM cards WHERE id=? AND user_id=?", (cid, uid)).fetchone()
        player = "Card"
        if row:
            try:
                player = json.loads(row["data"]).get("player") or "Card"
            except Exception:
                pass
        people = []
        for x in uids:
            p = person_of(con, x)
            if p:
                people.append(p)
        if people:
            cards.append({"id": cid, "player": player, "people": people})
    con.close()
    return {"followers": followers, "binder": binder, "cards": cards}

@app.get("/me")
async def me(request: Request, x_token: str | None = Header(default=None)):
    uid = require_user(request, x_token)
    slug = ensure_slug(uid)
    con = db()
    row = con.execute("SELECT email,display,hue,bio,avatar,cropx,cropy,cropz,socials FROM users WHERE id=?", (uid,)).fetchone()
    con.close()
    return {
        "slug": slug,
        "url": f"/?b={slug}",
        "operator": is_operator(uid),
        "display": (row["display"] if row else None) or "",
        "hue": (row["hue"] if row else None) or "#8fd4ee",
        "bio": (row["bio"] if row else None) or "",
        "avatar": (row["avatar"] if row else None) or "",
        "cropx": (row["cropx"] if row else None) or "50",
        "cropy": (row["cropy"] if row else None) or "50",
        "cropz": (row["cropz"] if row else None) or "100",
        "socials": parse_socials(row["socials"] if row else ""),
    }

@app.post("/profile")
async def save_profile(payload: dict, request: Request, x_token: str | None = Header(default=None)):
    uid = require_user(request, x_token)
    display = (payload.get("display") or "").strip()[:24]
    hue = (payload.get("hue") or "").strip()[:16] or "#8fd4ee"
    bio = (payload.get("bio") or "").strip()[:140]
    avatar = payload.get("avatar") or ""
    if payload.get("clear_avatar"):
        avatar = ""
    if isinstance(avatar, str) and len(avatar) > 600000:
        avatar = ""
    cropx = str(payload.get("cropx") or "50")[:4]
    cropy = str(payload.get("cropy") or "50")[:4]
    cropz = str(payload.get("cropz") or "100")[:4]
    socials = parse_socials(payload.get("socials") or {})
    if not display:
        raise HTTPException(400, "pick a screen name")
    con = db()
    con.execute(
        "UPDATE users SET display=?, hue=?, bio=?, avatar=?, cropx=?, cropy=?, cropz=?, socials=? WHERE id=?",
        (display, hue, bio, avatar, cropx, cropy, cropz, json.dumps(socials), uid),
    )
    con.commit()
    con.close()
    return {"ok": True, "display": display, "hue": hue, "bio": bio, "avatar": avatar, "socials": socials}

@app.get("/u/{slug}")
async def public_binder(slug: str, request: Request, x_token: str | None = Header(default=None)):
    slug = re.sub(r"[^a-z0-9]", "", (slug or "").lower())
    con = db()
    u = con.execute("SELECT id,display,hue,bio,avatar,avatar_hidden,cropx,cropy,cropz,socials,IFNULL(suspended,0) AS suspended FROM users WHERE slug=?", (slug,)).fetchone()
    if not u or int(u["suspended"] or 0):
        con.close()
        raise HTTPException(404, "binder not found")
    rows = con.execute("SELECT data FROM cards WHERE user_id=?", (u["id"],)).fetchall()
    raws = []
    for r in rows:
        try:
            raws.append(json.loads(r["data"]))
        except Exception:
            pass
    cards = [public_card(x) for x in raws]
    for c in cards:
        n = con.execute(
            "SELECT COUNT(*) AS n FROM likes WHERE slug=? AND card_id=?",
            (slug, c.get("id")),
        ).fetchone()["n"]
        c["likes"] = n
    blink = con.execute("SELECT COUNT(*) AS n FROM binder_likes WHERE slug=?", (slug,)).fetchone()["n"]
    tok = x_token or (request.headers.get("x-token") if request else None)
    me = user_from_token(tok)
    liked = False
    is_following = False
    if me:
        liked = bool(con.execute("SELECT user_id FROM binder_likes WHERE slug=? AND user_id=?", (slug, me)).fetchone())
        is_following = bool(con.execute("SELECT slug FROM follows WHERE follower=? AND slug=?", (me, slug)).fetchone())
    banners = con.execute(
        "SELECT id,kind,title,body,url,color,created FROM banner_posts WHERE user_id=? ORDER BY id DESC LIMIT 12",
        (u["id"],),
    ).fetchall()
    con.close()
    book = sum((card_market(x) or 0) for x in raws)
    grails = sum(1 for x in raws if is_grail(x))
    return {
        "slug": slug,
        "display": u["display"] or "Collector",
        "hue": u["hue"] or "#8fd4ee",
        "bio": u["bio"] or "",
        "socials": parse_socials(u["socials"] if "socials" in u.keys() else ""),
        "avatar": "" if int(u["avatar_hidden"] or 0) else (u["avatar"] or ""),
        "cropx": u["cropx"] or "50",
        "cropy": u["cropy"] or "50",
        "cropz": u["cropz"] or "100",
        "count": len(cards),
        "grails": grails,
        "book": book,
        "likes": blink,
        "liked": liked,
        "following": is_following,
        "cards": cards,
        "banners": [dict(b) for b in banners],
    }


def _clean_banner_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    if not re.match(r"^https?://[^\s]+$", url, re.I):
        return ""
    return url[:300]

SOCIAL_KEYS = ("instagram", "youtube", "x", "ebay", "tiktok", "site")
SOCIAL_PREFIX = {
    "instagram": "https://instagram.com/",
    "youtube": "https://youtube.com/",
    "x": "https://x.com/",
    "ebay": "https://www.ebay.com/usr/",
    "tiktok": "https://www.tiktok.com/@",
    "site": "https://",
}

def parse_socials(raw) -> dict:
    data = {}
    if isinstance(raw, str) and raw.strip():
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    for k in SOCIAL_KEYS:
        v = (raw.get(k) or "").strip()
        if not v:
            continue
        v = v.split()[0]
        if v.startswith("@"):
            v = v[1:]
        if not re.match(r"^https?://", v, re.I):
            v = SOCIAL_PREFIX[k] + v.lstrip("/")
        v = _clean_banner_url(v)
        if v:
            data[k] = v[:200]
    return data

@app.get("/banners")
async def my_banners(request: Request, x_token: str | None = Header(default=None)):
    uid = require_user(request, x_token)
    con = db()
    rows = con.execute(
        "SELECT id,kind,title,body,url,color,created FROM banner_posts WHERE user_id=? ORDER BY id DESC LIMIT 12",
        (uid,),
    ).fetchall()
    con.close()
    return {"banners": [dict(r) for r in rows]}


@app.post("/banners")
async def add_banner(payload: dict, request: Request, x_token: str | None = Header(default=None)):
    uid = require_user(request, x_token)
    kind = (payload.get("kind") or "note").strip()[:24]
    if kind not in ("show", "break", "episode", "live", "note"):
        kind = "note"
    title = (payload.get("title") or "").strip()[:80]
    body = (payload.get("body") or "").strip()[:280]
    url = _clean_banner_url(payload.get("url") or "")
    color = (payload.get("color") or "#8fd4ee").strip()[:16]
    if not re.match(r"^#[0-9a-fA-F]{3,8}$", color):
        color = "#8fd4ee"
    if len(title) < 2:
        title = (body or "")[:80]
    if len(title) < 2:
        raise HTTPException(400, "give the post a title")
    con = db()
    con.execute("DELETE FROM banner_posts WHERE user_id=?", (uid,))
    con.execute(
        "INSERT INTO banner_posts(user_id,kind,title,body,url,color,created) VALUES(?,?,?,?,?,?,?)",
        (uid, kind, title, body, url, color, time.strftime("%Y-%m-%dT%H:%M:%SZ")),
    )
    con.commit()
    rid = con.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    con.close()
    return {"ok": True, "id": rid}


@app.delete("/banners/{bid}")
async def del_banner(bid: int, request: Request, x_token: str | None = Header(default=None)):
    uid = require_user(request, x_token)
    con = db()
    con.execute("DELETE FROM banner_posts WHERE id=? AND user_id=?", (bid, uid))
    con.commit()
    con.close()
    return {"ok": True}

def decorate_comment(con, r) -> dict:
    item = dict(r)
    uid = item.get("user_id")
    u = con.execute(
        "SELECT slug, display, IFNULL(avatar_hidden,0) AS avatar_hidden, avatar FROM users WHERE id=?",
        (uid,),
    ).fetchone() if uid else None
    item["author_slug"] = (u["slug"] if u else "") or ""
    item["name"] = (u["display"] if u and u["display"] else item.get("name")) or "Collector"
    item["has_avatar"] = bool(u and u["avatar"] and not int(u["avatar_hidden"] or 0))
    return item

@app.get("/u/{slug}/cards/{cid}/comments")
async def list_comments(slug: str, cid: str):
    con = db()
    rows = con.execute(
        "SELECT id,name,body,created,user_id,IFNULL(parent_id,0) AS parent_id FROM comments WHERE slug=? AND card_id=? AND IFNULL(hidden,0)=0 AND IFNULL(parent_id,0)=0 ORDER BY id DESC LIMIT 80",
        (slug, cid),
    ).fetchall()
    out = []
    for r in rows:
        item = decorate_comment(con, r)
        item["replies"] = con.execute(
            "SELECT COUNT(*) AS n FROM comments WHERE parent_id=? AND IFNULL(hidden,0)=0",
            (r["id"],),
        ).fetchone()["n"]
        out.append(item)
    con.close()
    return {"comments": out}

@app.get("/comments/{cid}/replies")
async def list_replies(cid: int):
    con = db()
    rows = con.execute(
        "SELECT id,name,body,created,user_id,parent_id FROM comments WHERE parent_id=? AND IFNULL(hidden,0)=0 ORDER BY id ASC LIMIT 80",
        (cid,),
    ).fetchall()
    out = [decorate_comment(con, r) for r in rows]
    con.close()
    return {"comments": out}

@app.post("/u/{slug}/cards/{cid}/comments")
async def add_comment(slug: str, cid: str, payload: dict, request: Request, x_token: str | None = Header(default=None)):
    uid = require_user(request, x_token)
    body = (payload.get("body") or "").strip()
    parent_id = int(payload.get("parent_id") or 0)
    if len(body) < 2 or len(body) > 280:
        raise HTTPException(400, "comment 2–280 characters")
    con = db()
    owner = con.execute("SELECT id FROM users WHERE slug=?", (slug,)).fetchone()
    if not owner:
        con.close()
        raise HTTPException(404, "binder not found")
    card = con.execute("SELECT id FROM cards WHERE id=? AND user_id=?", (cid, owner["id"])).fetchone()
    if not card:
        con.close()
        raise HTTPException(404, "card not found")
    parent = None
    if parent_id:
        parent = con.execute(
            "SELECT id,user_id FROM comments WHERE id=? AND slug=? AND card_id=?",
            (parent_id, slug, cid),
        ).fetchone()
        if not parent:
            con.close()
            raise HTTPException(404, "comment not found")
    hour = time.strftime("%Y-%m-%dT%H")
    n = con.execute(
        "SELECT COUNT(*) AS n FROM comments WHERE user_id=? AND created LIKE ?",
        (uid, hour + "%"),
    ).fetchone()["n"]
    if n >= 12:
        con.close()
        raise HTTPException(429, "slow down — 12 comments an hour")
    name = display_of(con, uid)
    created = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    con.execute(
        "INSERT INTO comments(slug,card_id,user_id,name,body,created,parent_id) VALUES(?,?,?,?,?,?,?)",
        (slug, cid, uid, name, body, created, parent_id),
    )
    rid = con.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    notify_activity(
        con, owner["id"], slug,
        name+(" replied" if parent_id else " commented")+" on a card",
        cid, uid
    )
    if parent and parent["user_id"] not in (uid, owner["id"]):
        add_note(con, parent["user_id"], slug, name+" replied to you", cid)
    con.commit()
    con.close()
    return {"ok": True, "id": rid, "name": name, "body": body, "created": created, "parent_id": parent_id}

@app.post("/u/{slug}/report")
async def report_user(slug: str, payload: dict, request: Request, x_token: str | None = Header(default=None)):
    uid = require_user(request, x_token)
    slug = re.sub(r"[^a-z0-9]", "", (slug or "").lower())
    reason = re.sub(r"\s+", " ", (payload.get("reason") or "").strip())[:180]
    if len(reason) < 3:
        raise HTTPException(400, "pick a reason")
    con = db()
    target = con.execute("SELECT id, email, display FROM users WHERE slug=?", (slug,)).fetchone()
    if not target:
        con.close()
        raise HTTPException(404, "binder not found")
    if target["id"] == uid:
        con.close()
        raise HTTPException(400, "you can't report your own binder")
    already = con.execute(
        "SELECT id FROM reports WHERE reporter=? AND reason LIKE ?",
        (uid, f"user:{slug}|%"),
    ).fetchone()
    if already:
        n = con.execute(
            "SELECT COUNT(DISTINCT reporter) AS n FROM reports WHERE reason LIKE ?",
            (f"user:{slug}|%",),
        ).fetchone()["n"]
        con.close()
        return {"ok": True, "reports": n, "already": True}
    con.execute(
        "INSERT INTO reports(slug,reporter,reason,created) VALUES(?,?,?,?)",
        (slug, uid, f"user:{slug}|{reason}", time.strftime("%Y-%m-%dT%H:%M:%SZ")),
    )
    n = con.execute(
        "SELECT COUNT(DISTINCT reporter) AS n FROM reports WHERE reason LIKE ?",
        (f"user:{slug}|%",),
    ).fetchone()["n"]
    who = email_of(uid)
    con.commit()
    con.close()
    send_mail(
        "Ice Ledger user report",
        f"Collector report\nBinder: {slug}\nDisplay: {target['display']}\nEmail: {target['email']}\nReporter: {who} (id {uid})\nReason: {reason}\nUnique reports: {n}\nLook them up in Owner tools. Accounts are not auto-deleted.",
    )
    return {"ok": True, "reports": n, "already": False}

@app.post("/comments/{cid}/report")
async def report_comment(cid: int, payload: dict, request: Request, x_token: str | None = Header(default=None)):
    uid = require_user(request, x_token)
    reason = re.sub(r"\s+", " ", (payload.get("reason") or "").strip())[:180]
    if len(reason) < 3:
        raise HTTPException(400, "pick a reason")
    con = db()
    row = con.execute("SELECT id,slug,body FROM comments WHERE id=?", (cid,)).fetchone()
    if not row:
        con.close()
        raise HTTPException(404, "gone")
    already = con.execute(
        "SELECT id FROM reports WHERE reporter=? AND reason LIKE ?",
        (uid, f"comment:{cid}|%"),
    ).fetchone()
    if already:
        n = con.execute(
            "SELECT COUNT(DISTINCT reporter) AS n FROM reports WHERE reason LIKE ?",
            (f"comment:{cid}|%",),
        ).fetchone()["n"]
        con.close()
        return {"ok": True, "reports": n, "already": True}
    con.execute(
        "INSERT INTO reports(slug,reporter,reason,created) VALUES(?,?,?,?)",
        (row["slug"], uid, f"comment:{cid}|{reason}", time.strftime("%Y-%m-%dT%H:%M:%SZ")),
    )
    n = con.execute(
        "SELECT COUNT(DISTINCT reporter) AS n FROM reports WHERE reason LIKE ?",
        (f"comment:{cid}|%",),
    ).fetchone()["n"]
    hidden = False
    if n >= 3:
        con.execute("UPDATE comments SET hidden=1 WHERE id=?", (cid,))
        hidden = True
    who = email_of(uid)
    con.commit()
    con.close()
    send_mail(
        "Ice Ledger comment report",
        f"Comment report\nComment id: {cid}\nBinder: {row['slug']}\nReporter: {who} (id {uid})\nReason: {reason}\nText: {(row['body'] or '')[:200]}\nUnique reports: {n}\n(3 unique reports hide the comment.)",
    )
    if hidden:
        send_mail(
            "Ice Ledger comment removed",
            f"Comment HIDDEN after 3 unique reports.\nComment id: {cid}\nBinder: {row['slug']}\nText: {(row['body'] or '')[:200]}\nLast reason: {reason}\nRestore it from Owner tools → Removed if this was junk reporting.",
        )
    return {"ok": True, "reports": n, "already": False, "removed": hidden}

@app.get("/admin/inbox")
async def admin_inbox(request: Request, secret: str = "", x_token: str | None = Header(default=None)):
    if secret and APP_SECRET and secret == APP_SECRET:
        pass
    else:
        require_operator(request, x_token)
    con = db()
    rows = con.execute("SELECT id,slug,reporter,reason,created FROM reports ORDER BY id DESC LIMIT 80").fetchall()
    photos = con.execute(
        "SELECT slug, display FROM users WHERE IFNULL(avatar_hidden,0)=1 AND slug IS NOT NULL AND slug != ''"
    ).fetchall()
    comments = con.execute(
        "SELECT id, slug, card_id, body, user_id FROM comments WHERE IFNULL(hidden,0)=1 ORDER BY id DESC LIMIT 80"
    ).fetchall()
    reports = []
    for r in rows:
        item = dict(r)
        reason = item.get("reason") or ""
        rep = con.execute("SELECT email, display, slug FROM users WHERE id=?", (item.get("reporter"),)).fetchone()
        item["reporter_email"] = (rep["email"] if rep else "") or ""
        item["reporter_name"] = (rep["display"] if rep else "") or (rep["slug"] if rep else "") or ("user "+str(item.get("reporter")))
        if reason.startswith("user:"):
            parts = reason[5:].split("|", 1)
            who_slug = re.sub(r"[^a-z0-9]", "", (parts[0] or item.get("slug") or "").lower())
            item["kind"] = "user"
            item["slug"] = who_slug
            item["why"] = parts[1] if len(parts) > 1 else reason
            tgt = con.execute("SELECT display, email, IFNULL(suspended,0) AS suspended FROM users WHERE slug=?", (who_slug,)).fetchone()
            item["target_name"] = (tgt["display"] if tgt else "") or who_slug
            item["preview"] = "Collector · "+((tgt["email"] if tgt else "") or who_slug)
            item["hidden"] = int(tgt["suspended"] if tgt else 0)
            item["report_n"] = con.execute(
                "SELECT COUNT(DISTINCT reporter) AS n FROM reports WHERE reason LIKE ?",
                (f"user:{who_slug}|%",),
            ).fetchone()["n"]
        elif reason.startswith("comment:"):
            parts = reason[8:].split("|", 1)
            try:
                cid = int(parts[0])
            except Exception:
                cid = 0
            why = parts[1] if len(parts) > 1 else reason
            item["kind"] = "comment"
            item["comment_id"] = cid
            item["why"] = why
            rowc = con.execute("SELECT slug, card_id, body, IFNULL(hidden,0) AS hidden FROM comments WHERE id=?", (cid,)).fetchone()
            if rowc:
                item["slug"] = rowc["slug"]
                item["card_id"] = rowc["card_id"]
                item["preview"] = rowc["body"]
                item["hidden"] = int(rowc["hidden"] or 0)
            item["report_n"] = con.execute(
                "SELECT COUNT(DISTINCT reporter) AS n FROM reports WHERE reason LIKE ?",
                (f"comment:{cid}|%",),
            ).fetchone()["n"]
        else:
            item["kind"] = "photo"
            item["why"] = reason
            tgt = con.execute("SELECT display, IFNULL(avatar_hidden,0) AS hidden FROM users WHERE slug=?", (item.get("slug"),)).fetchone()
            item["target_name"] = (tgt["display"] if tgt else "") or item.get("slug")
            item["hidden"] = int(tgt["hidden"] if tgt else 0)
            item["preview"] = "Profile photo · "+(item.get("slug") or "")
            item["report_n"] = con.execute(
                "SELECT COUNT(DISTINCT reporter) AS n FROM reports WHERE slug=? AND reason NOT LIKE 'comment:%'",
                (item.get("slug"),),
            ).fetchone()["n"]
        reports.append(item)
    users_n = con.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    sus_n = con.execute("SELECT COUNT(*) AS n FROM users WHERE IFNULL(suspended,0)=1").fetchone()["n"]
    con.close()
    return {
        "reports": reports,
        "hidden_photos": [dict(r) for r in photos],
        "hidden_comments": [dict(r) for r in comments],
        "stats": {"users": users_n, "suspended": sus_n, "reports": len(reports), "hidden_photos": len(photos), "hidden_comments": len(comments)},
    }

@app.post("/admin/restore-photo")
async def admin_restore_photo(payload: dict, request: Request, x_token: str | None = Header(default=None)):
    require_operator(request, x_token)
    slug = re.sub(r"[^a-z0-9]", "", (payload.get("slug") or "").lower())
    con = db()
    con.execute("UPDATE users SET avatar_hidden=0 WHERE slug=?", (slug,))
    con.commit()
    con.close()
    send_mail("Ice Ledger photo restored", f"Profile photo restored for binder: {slug}")
    return {"ok": True}

@app.post("/admin/restore-comment")
async def admin_restore_comment(payload: dict, request: Request, x_token: str | None = Header(default=None)):
    require_operator(request, x_token)
    cid = int(payload.get("id") or 0)
    con = db()
    con.execute("UPDATE comments SET hidden=0 WHERE id=?", (cid,))
    con.commit()
    con.close()
    send_mail("Ice Ledger comment restored", f"Comment {cid} restored")
    return {"ok": True}

@app.post("/admin/hide-comment")
async def admin_hide(payload: dict, request: Request, x_token: str | None = Header(default=None)):
    require_operator(request, x_token)
    cid = int(payload.get("id") or 0)
    con = db()
    con.execute("UPDATE comments SET hidden=1 WHERE id=?", (cid,))
    con.commit()
    con.close()
    send_mail("Ice Ledger comment hidden", f"Comment {cid} hidden by operator")
    return {"ok": True}

@app.post("/admin/dismiss-report")
async def admin_dismiss(payload: dict, request: Request, x_token: str | None = Header(default=None)):
    require_operator(request, x_token)
    rid = int(payload.get("id") or 0)
    con = db()
    con.execute("DELETE FROM reports WHERE id=?", (rid,))
    con.commit()
    con.close()
    return {"ok": True}

@app.get("/admin/users")
async def admin_users(request: Request, x_token: str | None = Header(default=None)):
    require_operator(request, x_token)
    con = db()
    rows = con.execute(
        "SELECT id,email,slug,display,created,IFNULL(plan,'free') AS plan,IFNULL(suspended,0) AS suspended FROM users ORDER BY id DESC LIMIT 300"
    ).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        item["cards"] = con.execute("SELECT COUNT(*) AS n FROM cards WHERE user_id=?", (r["id"],)).fetchone()["n"]
        out.append(item)
    con.close()
    return {"users": out}

@app.get("/admin/lookup")
async def admin_lookup(q: str = "", request: Request = None, x_token: str | None = Header(default=None)):
    require_operator(request, x_token)
    raw = (q or "").strip().lower()
    if not raw:
        raise HTTPException(400, "email or slug")
    con = db()
    row = None
    if "@" in raw:
        row = con.execute("SELECT * FROM users WHERE email=?", (raw,)).fetchone()
    if not row:
        slug = re.sub(r"[^a-z0-9]", "", raw)
        row = con.execute("SELECT * FROM users WHERE slug=?", (slug,)).fetchone()
    if not row:
        con.close()
        raise HTTPException(404, "no user with that email or slug")
    uid = row["id"]
    slug = row["slug"] or ""
    cards = con.execute("SELECT COUNT(*) AS n FROM cards WHERE user_id=?", (uid,)).fetchone()["n"]
    comments = con.execute("SELECT COUNT(*) AS n FROM comments WHERE user_id=?", (uid,)).fetchone()["n"]
    hidden_c = con.execute("SELECT COUNT(*) AS n FROM comments WHERE user_id=? AND IFNULL(hidden,0)=1", (uid,)).fetchone()["n"]
    reports = con.execute("SELECT COUNT(*) AS n FROM reports WHERE slug=?", (slug,)).fetchone()["n"] if slug else 0
    ban = con.execute("SELECT id,title,body,url,color FROM banner_posts WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
    plan = plan_of(uid)
    usage = usage_of(uid)
    con.close()
    return {
        "id": uid,
        "email": row["email"],
        "slug": slug,
        "display": row["display"] or "",
        "created": row["created"] or "",
        "suspended": int(row["suspended"] or 0) if "suspended" in row.keys() else 0,
        "avatar_hidden": int(row["avatar_hidden"] or 0) if "avatar_hidden" in row.keys() else 0,
        "has_avatar": bool(row["avatar"]),
        "plan": plan,
        "usage": usage,
        "cards": cards,
        "comments": comments,
        "hidden_comments": hidden_c,
        "reports": reports,
        "banner": dict(ban) if ban else None,
    }

@app.post("/admin/clear-banner")
async def admin_clear_banner(payload: dict, request: Request, x_token: str | None = Header(default=None)):
    require_operator(request, x_token)
    slug = re.sub(r"[^a-z0-9]", "", (payload.get("slug") or "").lower())
    con = db()
    u = con.execute("SELECT id FROM users WHERE slug=?", (slug,)).fetchone()
    if not u:
        con.close()
        raise HTTPException(404, "binder not found")
    con.execute("DELETE FROM banner_posts WHERE user_id=?", (u["id"],))
    con.commit()
    con.close()
    send_mail("Ice Ledger banner removed", f"Banner cleared for {slug}")
    return {"ok": True}

@app.post("/admin/hide-user-comments")
async def admin_hide_user_comments(payload: dict, request: Request, x_token: str | None = Header(default=None)):
    require_operator(request, x_token)
    on = 1 if payload.get("on", True) else 0
    slug = re.sub(r"[^a-z0-9]", "", (payload.get("slug") or "").lower())
    con = db()
    u = con.execute("SELECT id FROM users WHERE slug=?", (slug,)).fetchone()
    if not u:
        con.close()
        raise HTTPException(404, "binder not found")
    con.execute("UPDATE comments SET hidden=? WHERE user_id=?", (on, u["id"]))
    con.commit()
    con.close()
    return {"ok": True, "hidden": bool(on)}

@app.post("/admin/hide-photo")
async def admin_hide_photo(payload: dict, request: Request, x_token: str | None = Header(default=None)):
    require_operator(request, x_token)
    slug = re.sub(r"[^a-z0-9]", "", (payload.get("slug") or "").lower())
    con = db()
    con.execute("UPDATE users SET avatar_hidden=1 WHERE slug=?", (slug,))
    con.commit()
    con.close()
    send_mail("Ice Ledger photo hidden", f"Profile photo hidden for {slug}")
    return {"ok": True}

def _find_user(con, payload: dict):
    email = (payload.get("email") or "").strip().lower()
    slug = re.sub(r"[^a-z0-9]", "", (payload.get("slug") or "").lower())
    row = None
    if email:
        row = con.execute("SELECT id,email,slug FROM users WHERE email=?", (email,)).fetchone()
    if not row and slug:
        row = con.execute("SELECT id,email,slug FROM users WHERE slug=?", (slug,)).fetchone()
    return row

@app.post("/admin/suspend")
async def admin_suspend(payload: dict, request: Request, x_token: str | None = Header(default=None)):
    oid = require_operator(request, x_token)
    on = 1 if payload.get("on", True) else 0
    con = db()
    row = _find_user(con, payload)
    if not row:
        con.close()
        raise HTTPException(404, "no user with that email or slug")
    if row["id"] == oid:
        con.close()
        raise HTTPException(400, "don't suspend your own account")
    con.execute("UPDATE users SET suspended=? WHERE id=?", (on, row["id"]))
    if on:
        con.execute("DELETE FROM sessions WHERE user_id=?", (row["id"],))
    con.commit()
    con.close()
    send_mail("Ice Ledger account "+("suspended" if on else "restored"), f"{row['email']} / {row['slug']}")
    return {"ok": True, "email": row["email"], "slug": row["slug"], "suspended": bool(on)}

@app.post("/admin/delete-user")
async def admin_delete_user(payload: dict, request: Request, x_token: str | None = Header(default=None)):
    oid = require_operator(request, x_token)
    con = db()
    row = _find_user(con, payload)
    if not row:
        con.close()
        raise HTTPException(404, "no user with that email or slug")
    if row["id"] == oid:
        con.close()
        raise HTTPException(400, "don't delete your own account")
    uid = row["id"]
    slug = row["slug"] or ""
    con.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
    con.execute("DELETE FROM cards WHERE user_id=?", (uid,))
    con.execute("DELETE FROM comments WHERE user_id=?", (uid,))
    con.execute("DELETE FROM likes WHERE user_id=?", (uid,))
    con.execute("DELETE FROM binder_likes WHERE user_id=?", (uid,))
    con.execute("DELETE FROM follows WHERE follower=?", (uid,))
    con.execute("DELETE FROM notes WHERE user_id=?", (uid,))
    con.execute("DELETE FROM banner_posts WHERE user_id=?", (uid,))
    if slug:
        con.execute("DELETE FROM follows WHERE slug=?", (slug,))
        con.execute("DELETE FROM binder_likes WHERE slug=?", (slug,))
        con.execute("DELETE FROM reports WHERE slug=?", (slug,))
    con.execute("DELETE FROM users WHERE id=?", (uid,))
    con.commit()
    con.close()
    send_mail("Ice Ledger account deleted", f"{row['email']} / {slug} removed")
    return {"ok": True}

@app.post("/me/delete")
async def me_delete(payload: dict, request: Request, x_token: str | None = Header(default=None)):
    uid = require_user(request, x_token)
    pw = payload.get("password") or payload.get("pw") or ""
    con = db()
    row = con.execute("SELECT id,email,slug,pw FROM users WHERE id=?", (uid,)).fetchone()
    if not row or not check_pw(pw, row["pw"]):
        con.close()
        raise HTTPException(401, "wrong password")
    slug = row["slug"] or ""
    con.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
    con.execute("DELETE FROM cards WHERE user_id=?", (uid,))
    con.execute("DELETE FROM comments WHERE user_id=?", (uid,))
    con.execute("DELETE FROM likes WHERE user_id=?", (uid,))
    con.execute("DELETE FROM binder_likes WHERE user_id=?", (uid,))
    con.execute("DELETE FROM follows WHERE follower=?", (uid,))
    con.execute("DELETE FROM notes WHERE user_id=?", (uid,))
    con.execute("DELETE FROM banner_posts WHERE user_id=?", (uid,))
    if slug:
        con.execute("DELETE FROM follows WHERE slug=?", (slug,))
        con.execute("DELETE FROM binder_likes WHERE slug=?", (slug,))
        con.execute("DELETE FROM reports WHERE slug=?", (slug,))
    con.execute("DELETE FROM users WHERE id=?", (uid,))
    con.commit()
    con.close()
    send_mail("Ice Ledger account deleted", f"{row['email']} / {slug} deleted their account")
    return {"ok": True}

@app.delete("/comments/{cid}")
async def del_comment(cid: int, request: Request, x_token: str | None = Header(default=None)):
    uid = require_user(request, x_token)
    slug = ensure_slug(uid)
    con = db()
    row = con.execute("SELECT slug,user_id FROM comments WHERE id=?", (cid,)).fetchone()
    if not row:
        con.close()
        raise HTTPException(404, "gone")
    if row["user_id"] != uid and row["slug"] != slug:
        con.close()
        raise HTTPException(403, "not yours")
    con.execute("UPDATE comments SET hidden=1 WHERE id=?", (cid,))
    con.commit()
    con.close()
    return {"ok": True}
