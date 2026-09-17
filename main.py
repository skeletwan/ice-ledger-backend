import os, json, base64, time, re, sqlite3, hashlib, secrets, hmac
from io import BytesIO
from fastapi import FastAPI, UploadFile, File, Header, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse, Response
from urllib.parse import urlparse
from pathlib import Path
from PIL import Image
import httpx

XAI_API_KEY = os.environ.get("XAI_API_KEY", "")
APP_SECRET = os.environ.get("APP_SECRET", "")
MODEL = os.environ.get("XAI_MODEL", "grok-4-1-fast-non-reasoning")
DAILY_CAP = int(os.environ.get("DAILY_CAP", "80"))
FREE_SCANS = int(os.environ.get("FREE_SCANS", "10"))
PLUS_SCANS = int(os.environ.get("PLUS_SCANS", "200"))
FREE_BOOK = int(os.environ.get("FREE_BOOK", "8"))
PLUS_BOOK = int(os.environ.get("PLUS_BOOK", "30"))
STRIPE_PAY_LINK = os.environ.get("STRIPE_PAY_LINK", "")

app = FastAPI(title="Ice Ledger Identify")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_hits = {}

PROMPT = """Identify this HOCKEY trading card. Return ONLY JSON, no markdown.
Hockey only (NHL / CHL / IIHF / Team Canada). If it is not hockey, still fill what you see and set notes.
You may get FRONT and sometimes BACK. Back is source of truth for year, set name, card number, copyright line.
Slab: read the grading label first (grader, grade, cert), then the card through the case.

Upper Deck hockey rules (2015–2026 especially):
- Young Guns = insert "Young Guns" (not a parallel). Canvas Young Guns = insert "Young Guns Canvas".
- Exclusives, High Gloss, Clear Cut, Outburst, Traxx are parallels or separate inserts — never label a plain YG as those.
- "C" or Young Guns badge on silver UD Series 1/2 rookies is usually Young Guns, not SP Authentic.
- Copy set name from the back: Series 1, Series 2, Extended, SP Authentic, SP Game Used, The Cup, Stature, Premier, Allure, Synergy, Metal Universe, Chronology, Trilogy, O-Pee-Chee, Parkhurst.
- Parallel examples: Silver Foil, Gold /100, Exclusives /100, High Gloss /10, Clear Cut, Outburst Gold, Rainbow, Black /1. If no /n and no foil name, parallel is null or Base.
- Do not invent a numbered parallel because the photo is shiny.

If a field is not readable, use null. Never invent a rare parallel.
{
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
    return {"ok": True, "model": MODEL, "key_set": bool(XAI_API_KEY)}

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
    return data


COMP_MODEL = os.environ.get("COMP_MODEL", "grok-4-1-fast-reasoning")
COMP_PROMPT = """Search recent SOLD / completed hockey card sales for this exact card (not asking prices).
Prefer eBay sold and 130point.com.
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
Fill each *_cad field you can. suggested_cad is the price for THIS copy's grader/grade.
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
    async with httpx.AsyncClient(timeout=120) as client:
        for model in (COMP_MODEL, "grok-4-1-fast", MODEL):
            r = await client.post(
                "https://api.x.ai/v1/responses",
                headers=headers,
                json={"model": model, "tools": [{"type": "web_search"}], "input": prompt},
            )
            if r.status_code < 400:
                used = model
                text = _extract_response_text(r.json()).strip()
                if text:
                    break
            err = f"{r.status_code}: {r.text[:180]}"
        if not text:
            r2 = await client.post(
                "https://api.x.ai/v1/chat/completions",
                headers=headers,
                json={"model": MODEL, "temperature": 0, "messages": [{"role": "user", "content": prompt}]},
            )
            if r2.status_code < 400:
                used = MODEL
                text = _extract_response_text(r2.json()).strip()
            else:
                err = err or f"{r2.status_code}: {r2.text[:180]}"
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
    for col, spec in (("display", "TEXT"), ("hue", "TEXT"), ("bio", "TEXT")):
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
    con.execute("""
    CREATE TABLE IF NOT EXISTS likes (
      slug TEXT NOT NULL,
      card_id TEXT NOT NULL,
      user_id INTEGER NOT NULL,
      created TEXT NOT NULL,
      PRIMARY KEY (slug, card_id, user_id)
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

def public_card(raw: dict) -> dict:
    c = dict(raw or {})
    for k in ("cost", "notes", "scan"):
        c.pop(k, None)
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
        "photo": c.get("photo"),
        "comp": c.get("comp"),
        "book": c.get("book") or {},
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
    return uid

def month_key():
    return time.strftime("%Y-%m")

def plan_of(uid: int) -> str:
    con = db()
    row = con.execute("SELECT plan FROM users WHERE id=?", (uid,)).fetchone()
    con.close()
    p = (row["plan"] if row else "free") or "free"
    return p

def usage_of(uid: int):
    m = month_key()
    plan = plan_of(uid)
    cap = PLUS_SCANS if plan == "plus" else FREE_SCANS
    bcap = PLUS_BOOK if plan == "plus" else FREE_BOOK
    con = db()
    row = con.execute("SELECT n FROM usage WHERE user_id=? AND month=?", (uid, m)).fetchone()
    brow = con.execute("SELECT n FROM book_usage WHERE user_id=? AND month=?", (uid, m)).fetchone()
    con.close()
    used = row["n"] if row else 0
    bused = brow["n"] if brow else 0
    return {
        "used": used, "cap": cap, "left": max(0, cap - used),
        "book_used": bused, "book_cap": bcap, "book_left": max(0, bcap - bused),
        "plan": plan, "month": m
    }

def bump_usage(uid: int):
    m = month_key()
    con = db()
    row = con.execute("SELECT n FROM usage WHERE user_id=? AND month=?", (uid, m)).fetchone()
    if row:
        con.execute("UPDATE usage SET n=n+1 WHERE user_id=? AND month=?", (uid, m))
    else:
        con.execute("INSERT INTO usage(user_id,month,n) VALUES(?,?,1)", (uid, m))
    con.commit()
    con.close()

def bump_book(uid: int):
    m = month_key()
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
        raise HTTPException(402, "scan cap reached — upgrade")
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
    row = con.execute("SELECT id,pw FROM users WHERE email=?", (email,)).fetchone()
    if not row:
        con.close()
        raise HTTPException(401, "no account with that email")
    if not check_pw(pw, row["pw"]):
        con.close()
        raise HTTPException(401, "wrong password")
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
    if STRIPE_PAY_LINK:
        return {"url": STRIPE_PAY_LINK, "price": "8 CAD / month"}
    return {"url": None, "price": "8 CAD / month", "note": "Set STRIPE_PAY_LINK on Railway when the Stripe Payment Link is live."}

@app.post("/plus")
async def plus(payload: dict, request: Request, x_token: str | None = Header(default=None)):
    """Flip plan to plus using APP_SECRET until Stripe webhook exists."""
    if APP_SECRET and payload.get("secret") != APP_SECRET:
        raise HTTPException(401, "app secret does not match")
    uid = require_user(request, x_token)
    con = db()
    con.execute("UPDATE users SET plan='plus' WHERE id=?", (uid,))
    con.commit()
    con.close()
    return usage_of(uid)

@app.post("/reset")
async def reset(payload: dict):
    email = (payload.get("email") or "").strip().lower()
    pw = payload.get("password") or ""
    secret = payload.get("secret") or ""
    if APP_SECRET and secret != APP_SECRET:
        raise HTTPException(401, "app secret does not match")
    if "@" not in email or len(pw) < 6:
        raise HTTPException(400, "email and a new password (6+ characters)")
    con = db()
    row = con.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
    if not row:
        con.close()
        raise HTTPException(404, "no account with that email")
    con.execute("UPDATE users SET pw=? WHERE id=?", (hash_pw(pw), row["id"]))
    con.execute("DELETE FROM sessions WHERE user_id=?", (row["id"],))
    token = secrets.token_urlsafe(24)
    con.execute(
        "INSERT INTO sessions(token,user_id,created) VALUES(?,?,?)",
        (token, row["id"], time.strftime("%Y-%m-%dT%H:%M:%SZ")),
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
    return {"cards": [json.loads(r["data"]) for r in rows]}

@app.post("/cards")
async def upsert_card(payload: dict, request: Request, x_token: str | None = Header(default=None)):
    uid = require_user(request, x_token)
    card = payload.get("card") or payload
    cid = card.get("id") or ("c" + str(int(time.time()*1000)))
    card["id"] = cid
    # do not store giant data-urls if huge
    for k in ("scan", "photo"):
        v = card.get(k)
        if isinstance(v, str) and len(v) > 250000:
            card[k] = None
    con = db()
    con.execute(
        "INSERT OR REPLACE INTO cards(id,user_id,data) VALUES(?,?,?)",
        (cid, uid, json.dumps(card)),
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
    rows = con.execute(
        """
        SELECT u.slug, u.display, u.hue, u.bio, COUNT(c.id) AS n
        FROM users u
        JOIN cards c ON c.user_id = u.id
        WHERE u.slug IS NOT NULL AND u.slug != ''
        GROUP BY u.id
        HAVING n > 0
        ORDER BY n DESC
        LIMIT 80
        """
    ).fetchall()
    con.close()
    return {"binders": [{"slug": r["slug"], "display": r["display"] or r["slug"], "hue": r["hue"] or "#8fd4ee", "bio": r["bio"] or "", "count": r["n"]} for r in rows]}

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
    con.close()
    return {"likes": n, "liked": mine}

@app.get("/me")
async def me(request: Request, x_token: str | None = Header(default=None)):
    uid = require_user(request, x_token)
    slug = ensure_slug(uid)
    con = db()
    row = con.execute("SELECT display,hue,bio FROM users WHERE id=?", (uid,)).fetchone()
    con.close()
    return {
        "slug": slug,
        "url": f"/?b={slug}",
        "display": (row["display"] if row else None) or slug,
        "hue": (row["hue"] if row else None) or "#8fd4ee",
        "bio": (row["bio"] if row else None) or "",
    }

@app.post("/profile")
async def save_profile(payload: dict, request: Request, x_token: str | None = Header(default=None)):
    uid = require_user(request, x_token)
    display = (payload.get("display") or "").strip()[:24]
    hue = (payload.get("hue") or "").strip()[:16] or "#8fd4ee"
    bio = (payload.get("bio") or "").strip()[:140]
    if not display:
        raise HTTPException(400, "pick a display name")
    con = db()
    con.execute("UPDATE users SET display=?, hue=?, bio=? WHERE id=?", (display, hue, bio, uid))
    con.commit()
    con.close()
    return {"ok": True, "display": display, "hue": hue, "bio": bio}

@app.get("/u/{slug}")
async def public_binder(slug: str):
    slug = re.sub(r"[^a-z0-9]", "", (slug or "").lower())
    con = db()
    u = con.execute("SELECT id,display,hue,bio FROM users WHERE slug=?", (slug,)).fetchone()
    if not u:
        con.close()
        raise HTTPException(404, "binder not found")
    rows = con.execute("SELECT data FROM cards WHERE user_id=?", (u["id"],)).fetchall()
    con.close()
    cards = [public_card(json.loads(r["data"])) for r in rows]
    book = sum((c.get("comp") or 0) for c in cards if isinstance(c.get("comp"), (int, float)))
    return {
        "slug": slug,
        "display": u["display"] or slug,
        "hue": u["hue"] or "#8fd4ee",
        "bio": u["bio"] or "",
        "count": len(cards),
        "book": book,
        "cards": cards,
    }

@app.get("/u/{slug}/cards/{cid}/comments")
async def list_comments(slug: str, cid: str):
    con = db()
    rows = con.execute(
        "SELECT id,name,body,created FROM comments WHERE slug=? AND card_id=? ORDER BY id DESC LIMIT 80",
        (slug, cid),
    ).fetchall()
    con.close()
    return {"comments": [dict(r) for r in rows]}

@app.post("/u/{slug}/cards/{cid}/comments")
async def add_comment(slug: str, cid: str, payload: dict, request: Request, x_token: str | None = Header(default=None)):
    uid = require_user(request, x_token)
    body = (payload.get("body") or "").strip()
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
    hour = time.strftime("%Y-%m-%dT%H")
    n = con.execute(
        "SELECT COUNT(*) AS n FROM comments WHERE user_id=? AND created LIKE ?",
        (uid, hour + "%"),
    ).fetchone()["n"]
    if n >= 12:
        con.close()
        raise HTTPException(429, "slow down — 12 comments an hour")
    name = ensure_slug(uid)
    created = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    con.execute(
        "INSERT INTO comments(slug,card_id,user_id,name,body,created) VALUES(?,?,?,?,?,?)",
        (slug, cid, uid, name, body, created),
    )
    con.commit()
    rid = con.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    con.close()
    return {"ok": True, "id": rid, "name": name, "body": body, "created": created}

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
    con.execute("DELETE FROM comments WHERE id=?", (cid,))
    con.commit()
    con.close()
    return {"ok": True}
