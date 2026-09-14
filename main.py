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

app = FastAPI(title="Ice Ledger Identify")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_hits = {}

PROMPT = """Identify this hockey trading card. Return ONLY JSON, no markdown.
You may get a FRONT image and sometimes a BACK image. Use the back for year, set, card number, copyright line.
If it is a graded slab, read the label first. If raw, use front for player/parallel and back for set/year/number.
If a field is not readable, use null. Do not invent a rare parallel.
insert examples: Young Guns, SP Authentic, Exclusives, Canvas, Clear Cut, base, insert.
{
  "player": string|null,
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
    secret: str | None = Form(default=None),
):
    check_secret(x_app_secret or secret)
    if not XAI_API_KEY:
        raise HTTPException(500, "XAI_API_KEY not set on server")
    check_cap()
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
  "low": number|null,
  "high": number|null,
  "currency": "CAD"|"USD"|null,
  "sample_count": number,
  "confidence": number,
  "needs_review": boolean,
  "summary": string,
  "sources": [string]
}}
Always fill suggested_usd or suggested_cad if you see ANY sold prices.
Use the median of matching solds. Convert USD to CAD at 1.35 for suggested_cad.
If matches are messy, still fill the number and set needs_review true.
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
):
    check_secret(x_app_secret or payload.get("secret"))
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
    nums = []
    for key in ("suggested_cad", "suggested_usd", "low", "high"):
        try:
            if data.get(key) is not None:
                nums.append(float(data[key]))
        except (TypeError, ValueError):
            pass
    for m in re.findall(r"\$?\s*([0-9]{1,5}(?:\.[0-9]{1,2})?)", text or ""):
        v = float(m)
        if 2 <= v <= 20000:
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
):
    check_secret(x_app_secret or payload.get("secret"))
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
    """)
    con.commit()
    con.close()

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

@app.post("/signup")
async def signup(payload: dict):
    email = (payload.get("email") or "").strip().lower()
    pw = payload.get("password") or ""
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
    pw = payload.get("password") or ""
    con = db()
    row = con.execute("SELECT id,pw FROM users WHERE email=?", (email,)).fetchone()
    if not row or not check_pw(pw, row["pw"]):
        con.close()
        raise HTTPException(401, "bad email or password")
    token = secrets.token_urlsafe(24)
    con.execute("INSERT INTO sessions(token,user_id,created) VALUES(?,?,?)",
                (token, row["id"], time.strftime("%Y-%m-%dT%H:%M:%SZ")))
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
