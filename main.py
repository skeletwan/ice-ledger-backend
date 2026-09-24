import os, json, base64, time, re, sqlite3, hashlib, secrets, hmac
from datetime import datetime, timedelta, timezone
try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("America/Toronto")
except Exception:
    TZ = timezone(timedelta(hours=-4))
from io import BytesIO
from fastapi import FastAPI, UploadFile, File, Header, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse, Response
from urllib.parse import urlparse, quote_plus
from pathlib import Path
from PIL import Image, ImageOps
import httpx
from catalog import ensure_catalog, catalog_matches, vote_card, catalog_stats, catalog_parallel_terms

XAI_API_KEY = os.environ.get("XAI_API_KEY", "")
APP_SECRET = os.environ.get("APP_SECRET", "")
OPERATOR_EMAIL = (os.environ.get("OPERATOR_EMAIL", "hello@clapperspc.com") or "hello@clapperspc.com").lower()
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

MAIL_TO = _env("MAIL_TO", "hello@clapperspc.com")
MAIL_FROM = _env("MAIL_FROM", "Clappers PC <noreply@contact.clapperspc.com>")
RESEND_API_KEY = _env("RESEND_API_KEY")
SMTP_HOST = _env("SMTP_HOST")
SMTP_PORT = int(_env("SMTP_PORT", "587") or "587")
SMTP_USER = _env("SMTP_USER")
SMTP_PASS = _env("SMTP_PASS")
APP_URL = _env("APP_URL")
CARD_API_KEY = _env("CARD_API_KEY")
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
    sender = MAIL_FROM or f"Clappers PC <{MAIL_TO}>"
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

app = FastAPI(title="Clappers PC Identify")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_hits = {}

PROMPT = """Identify this trading card. Clappers PC only catalogs HOCKEY right now.
Hockey = NHL, AHL, CHL (OHL/WHL/QMJHL), IIHF, Team Canada/USA, PWHL, junior/international hockey.
If the card is baseball, basketball, football, soccer, Pokemon, TCG, entertainment, or anything else: set hockey false and sport to that category. Do not pretend it is hockey.
Return ONLY JSON, no markdown.
You may get FRONT and sometimes BACK. Back is source of truth for year, set name, card number, copyright line.
Slab: read the grading label first (grader, grade, cert), then the card through the case. Graders include PSA, BGS, SGC, CGC, SMA.

Upper Deck hockey rules (2015–2026 especially):
- Young Guns = insert "Young Guns" ONLY when the front says YOUNG GUNS or shows the YG shield on flagship UD Series 1 / Series 2 / Extended. It is not a synonym for Rookie or RC.
- RC / Rookie / "Rookie Card" on Sizzle Reel, Allure, SP Authentic, Future Watch, Artifacts, The Cup, Stature, Premier, Synergy, Metal Universe, OPC, Parkhurst, Portraits, Dazzlers, Holofoil, Ice, Black Diamond is NOT Young Guns. Use that product name as insert or set.
- Young Guns Renewed is insert "Young Guns Renewed". Canvas Young Guns is insert "Young Guns Canvas".
- "C" badge alone is not enough. Series 1 #1–200 and Series 2 #251–450 are base (or that insert), not Young Guns. YG checklist numbers are typically 201–250 (S1), 451–500 (S2), and the Extended YG range.
- Exclusives, High Gloss, Clear Cut, Outburst, Traxx are parallels or separate inserts — never label a plain YG as those.
- Silver UD flagship with YOUNG GUNS printed or the YG logo = Young Guns. Silver UD without those words = base or the insert actually printed (Canvas, Sizzle Reel, Portraits).
- Young Guns Deluxe / DELUXE on a UD rookie: insert stays Young Guns. Parallel is Deluxe /250 (or the printed serial). Set is Series 1, Series 2, or Extended from the back.
- Flagship UD Series 1/2/Extended rainbow (base AND Young Guns): Outburst Silver, Clear Cut, Deluxe /250, UD Exclusives /100, Outburst Red /25, High Gloss /10, Outburst Gold 1/1, Printing Plates 1/1.
- Common UD inserts (not parallels): UD Canvas, UD Canvas Young Guns, UD Portraits, Dazzlers (Blue/Pink/Green/Gold), Encore, Population Count, Sizzle Reel, Young Guns Renewed, Holotypes, OPC Glossy, French.
- SP Authentic: Future Watch, Auto Patch. The Cup: Rookie Auto Patch. Stature / Premier / Allure / Synergy / Metal Universe / Chronology / Trilogy have their own numbered color rainbows — copy the name on the card.
- Upper Deck Splendor: set is Splendor. Almost always numbered. Visible 12/49, /99, /25, /10, 1/1 must go in parallel. Number is checklist only. Do not return a Splendor as Base.
- Copy set name from the back: Series 1, Series 2, Extended, SP Authentic, SP Game Used, The Cup, Stature, Premier, Allure, Synergy, Metal Universe, Chronology, Trilogy, O-Pee-Chee, Parkhurst, Choice, Fleer Ultra, Skybox Impact.
- Vintage 1990s: never return only "Ultra" or only "Impact". Set must include the brand: Fleer Ultra, Skybox Impact, Score, Pinnacle, Donruss, Leaf, Topps, OPC, Stadium Club, Be A Player. Rookie / RC on those cards is insert "Rookie", not the set.
- Parallel examples: Silver Foil, Gold /100, Exclusives /100, High Gloss /10, Clear Cut, Outburst Gold, Rainbow, Black /1. If no /n and no foil name, parallel is null or Base.
- READ THE PHOTO FOR SERIALS. Look at every corner, the foil stamp, under the player, and the back. If you see digits with a slash (12/25, 003/100, 1/1) or SN25 / #'d /250, set serial to that exact text (keep the slash).
- Numbered print runs belong in parallel with the foil name: "Gold /25". Never put 12/25 in "number". number is checklist # only (#201).
- If serial is present, parallel must include it. Do not return serial=null when 12/25 is readable.
- "number" is only the checklist # on the back or bottom (e.g. 201, 144). Jersey number is not the card number unless no checklist # exists.
- If the front/back shows both a checklist # and a serial (12/25), number=checklist, parallel includes /25.
- Do not invent a numbered parallel because the photo is shiny.
- 1990s Pinnacle / Score / Donruss / Leaf: Starquest, Artist's Proofs, Rink Collection, Ice Breakers. Starquest color versions are parallels — Green, Red, Blue, Gold, Purple, Black. If the card face or foil is clearly green, parallel is "Green" (not Base). Same for other named colors.
- Set name Starquest (Pinnacle) is the set, not an insert, when the front says STARQUEST. Player still from the photo (e.g. Eric Lindros).
- Starquest Red is a red/maroon foil face — parallel MUST be Red. Green/Blue/Gold/Purple/Black same rule. Never Base when the plate is a color.
- Upper Deck Choice (1998–99 Choice, 1999–00 Choice, etc.): if the card says Choice Reserve, set is "Choice" (or the full year + Choice from the back) and parallel/insert is "Reserve" — do not call it Series 1 or a generic Upper Deck base. Choice Preview, Choice Reserve Mini, Choice StarQuest-style names stay as printed. Joe Thornton Choice Reserve is player Thornton, set Choice, parallel or insert Reserve.

Foil / color (required look):
- foil_color = dominant color of the foil or card stock if it is clearly not a normal white/cream base: Green, Red, Blue, Gold, Purple, Black, Silver, Bronze, Orange. "A bit shiny" is not a color. A green Starquest face is Green.
- foil_text = exact words you can read in the foil stamp or colored plate (CHOICE, RESERVE, STARQUEST, EXCLUSIVES, etc.). Copy them even if stylized.
- If foil_color is a named color and parallel is empty or Base, set parallel to that color.
- If foil_text includes RESERVE with Choice, parallel or insert is Reserve.

Autos:
- If you see a handwritten signature on the card or slab, or printed AUTO / AU / AUTograph / On-Card / Sticker Auto, set auto=true.
- Put "Autograph" in parallel (keep foil name too: "Gold /99 Autograph"). Do not call a signed card Base.
- Sticker vs on-card: if a sticker or certification label is obvious, parallel can say "Sticker Auto"; if ink is on the photo, "On-Card Auto".
- Ink on the slab label only (grader notes) is not an auto.

Memorabilia:
- If you see fabric, a jersey swatch, a patch, logo patch, prime patch, laundry tag, or relic window, set mem=true.
- Put the type in insert or parallel: Jersey, Patch, Prime Patch, Relic, Stick, Logo Patch. Keep /n and Autograph if present.
- Do not call a memorabilia card Base.

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
  "auto": boolean,
  "serial": string|null,
  "mem": boolean,
  "team": string|null,
  "grader": "Raw"|"PSA"|"BGS"|"SGC"|"CGC"|"SMA"|null,
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
PHOTO_DIR = Path(os.environ.get("PHOTO_DIR", str(DB_PATH.parent / "photos")))

def photo_path(uid, cid: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9._-]", "", str(cid or ""))[:80] or "card"
    return PHOTO_DIR / str(int(uid)) / (safe + ".jpg")

def decode_data_url(blob: str) -> bytes | None:
    if not isinstance(blob, str) or len(blob) < 80:
        return None
    raw = blob.split(",", 1)[-1] if blob.startswith("data:") else blob
    raw = re.sub(r"\s+", "", raw)
    try:
        data = base64.b64decode(raw)
    except Exception:
        return None
    return data if data and len(data) >= 32 else None

def save_card_photo(uid, cid: str, blob: str) -> bool:
    data = decode_data_url(blob)
    if not data:
        return False
    path = photo_path(uid, cid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return True

def photo_file_exists(uid, cid: str) -> bool:
    path = photo_path(uid, cid)
    try:
        return path.is_file() and path.stat().st_size >= 32
    except Exception:
        return False

def read_card_photo_bytes(uid, cid: str, raw: dict | None = None) -> tuple[bytes, str] | None:
    path = photo_path(uid, cid)
    if path.is_file():
        data = path.read_bytes()
        if len(data) >= 32:
            return data, "image/jpeg"
    blob = ""
    if raw:
        blob = raw.get("photo") or raw.get("scan") or ""
    data = decode_data_url(blob) if blob else None
    if not data:
        return None
    try:
        save_card_photo(uid, cid, blob)
    except Exception:
        pass
    kind = "image/png" if isinstance(blob, str) and "png" in blob[:40] else "image/jpeg"
    return data, kind

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

@app.get("/logo.jpg")
def logo_jpg():
    p = ROOT / "logo.jpg"
    if p.exists():
        return FileResponse(p, media_type="image/jpeg")
    raise HTTPException(404, "no logo")

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
    try:
        from PIL import ImageOps
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass
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

@app.get("/catalog/stats")
async def catalog_stats_ep():
    con = db()
    ensure_catalog(con)
    out = catalog_stats(con)
    con.close()
    return out

@app.get("/catalog/search")
async def catalog_search(q: str = "", year: str = "", player: str = ""):
    con = db()
    ensure_catalog(con)
    hits = catalog_matches(con, {"player": player or q, "year": year, "set": q, "number": "", "parallel": "", "insert": q})
    con.close()
    return {"matches": hits}

def scrub_false_yg(data: dict) -> bool:
    if not isinstance(data, dict):
        return False
    ins = str(data.get("insert") or "")
    st = str(data.get("set") or "")
    par = str(data.get("parallel") or "")
    before = ins
    mix = f"{st} {ins} {par}".lower()
    if not re.search(r"young guns|^\s*yg\s*$", ins, flags=re.I) and ins.lower() != "yg":
        return False
    named_ins = re.search(
        r"(sizzle reel|future watch|holofoil|dazzlers|portraits|young guns renewed|young guns canvas|canvas young guns)",
        mix,
    )
    named_set = re.search(
        r"(allure|sp authentic|the cup|artifacts|stature|premier|synergy|metal universe|trilogy|o-pee-chee|parkhurst|choice|black diamond|credentials|chronology|fleer ultra|skybox)",
        mix,
    )
    if named_ins:
        pretty = {
            "sizzle reel": "Sizzle Reel",
            "future watch": "Future Watch",
            "holofoil": "Holofoil",
            "dazzlers": "Dazzlers",
            "portraits": "Portraits",
            "young guns renewed": "Young Guns Renewed",
            "young guns canvas": "Young Guns Canvas",
            "canvas young guns": "Young Guns Canvas",
        }
        data["insert"] = pretty.get(named_ins.group(1), named_ins.group(1).title())
    elif named_set and not re.search(r"series\s*[123]|extended", st, flags=re.I):
        data["insert"] = "Rookie" if re.search(r"\brc\b|rookie", mix) else None
    return before != str(data.get("insert") or "")


def scrub_user_yg(uid: int) -> int:
    n = 0
    con = db()
    rows = con.execute("SELECT id, data FROM cards WHERE user_id=?", (uid,)).fetchall()
    for r in rows:
        try:
            card = json.loads(r["data"])
        except Exception:
            continue
        if not scrub_false_yg(card):
            continue
        con.execute("UPDATE cards SET data=? WHERE id=? AND user_id=?", (json.dumps(card), r["id"], uid))
        n += 1
        pl = str(card.get("player") or "").strip().lower()
        if pl:
            try:
                con.execute("DELETE FROM comp_cache WHERE k LIKE ?", (pl + "|%",))
            except Exception:
                pass
    con.commit()
    con.close()
    return n


def wipe_user_book(uid: int) -> int:
    keys = (
        "comp","sysComp","rawComp","psa6Comp","psa7Comp","psa8Comp","psa9Comp","psa10Comp",
        "bgs9Comp","bgs95Comp","bgs10Comp","bgsBlackComp","sgc10Comp","book","compAt",
    )
    n = 0
    con = db()
    rows = con.execute("SELECT id, data FROM cards WHERE user_id=?", (uid,)).fetchall()
    for r in rows:
        try:
            card = json.loads(r["data"])
        except Exception:
            continue
        for k in keys:
            card.pop(k, None)
        card["comp"] = 1
        card["rawComp"] = 1
        card["sysComp"] = 1
        card["book"] = {"raw_cad": 1, "suggested_cad": 1}
        card["hist"] = []
        card["histMap"] = {}
        con.execute("UPDATE cards SET data=? WHERE id=? AND user_id=?", (json.dumps(card), r["id"], uid))
        n += 1
    pl_seen = set()
    for r in rows:
        try:
            pl = str(json.loads(r["data"]).get("player") or "").strip().lower()
        except Exception:
            continue
        if pl and pl not in pl_seen:
            pl_seen.add(pl)
            con.execute("DELETE FROM comp_cache WHERE k LIKE ?", (pl + "|%",))
            con.execute("DELETE FROM house_solds WHERE fp LIKE ?", (pl + "|%",))
    try:
        con.execute("UPDATE users SET book_hist=? WHERE id=?", ("[]", uid))
    except Exception:
        pass
    con.commit()
    con.close()
    return n

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
    if "starquest" in blob or "star quest" in blob:
        if "starquest" not in st.lower() and "star quest" not in st.lower():
            data["set"] = "Starquest"
            st = "Starquest"
        if named and named.lower() not in (par or "").lower():
            data["parallel"] = named
            par = named
        elif re.search(r"\b(red|green|blue|gold|purple|black)\b", blob) and (not par or par.lower() in ("base","null","none")):
            col = re.search(r"\b(red|green|blue|gold|purple|black)\b", blob)
            data["parallel"] = col.group(1).title()
            par = data["parallel"]
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
    serial = str(data.get("serial") or "").strip()
    num = str(data.get("number") or "").strip()
    ser = re.search(r"(\d{1,4}\s*/\s*\d{1,4}|/\s*\d{1,4}|\bSN\s*\d{1,4})", serial + " " + num + " " + par, flags=re.I)
    if ser:
        run = re.sub(r"\s+", "", ser.group(0).upper().replace("SN", "/")) if ser.group(0).upper().startswith("SN") else ser.group(0).replace(" ", "")
        if run.lower() not in (par or "").lower():
            data["parallel"] = ((par + " " + run).strip() if par and par.lower() not in ("base", "null", "none") else run)
            par = data["parallel"]
        if re.search(r"\d+\s*/\s*\d+", num):
            data["number"] = re.sub(r"\s*\d{1,4}\s*/\s*\d{1,4}\b", "", num).strip(" -#") or None
    num = str(data.get("number") or "").strip()
    serial = re.search(r"(\d{1,3})\s*/\s*(\d{1,3})\b", num) or re.search(r"/\s*(\d{1,3})\b", num)
    if serial:
        run = serial.group(0).replace(" ", "")
        data["number"] = re.sub(r"\s*\d{1,3}\s*/\s*\d{1,3}\b", "", num).strip(" -#") or None
        if run.lower() not in par.lower():
            data["parallel"] = (par + " " + run).strip() if par and par.lower() not in ("base", "null", "none") else run
            par = data["parallel"]
    mem_hit = bool(data.get("mem")) or re.search(r"\b(patch|jersey|relic|memorabilia|swatch|prime patch|logo patch|laundry)\b", blob)
    if mem_hit:
        data["mem"] = True
        mix = (par + " " + ins).lower()
        kind = "Patch" if "patch" in blob else ("Jersey" if "jersey" in blob or "swatch" in blob else ("Relic" if "relic" in blob else "Memorabilia"))
        if "patch" not in mix and "jersey" not in mix and "relic" not in mix and "memorabilia" not in mix:
            if not ins or ins.lower() in ("base", "null", "none"):
                data["insert"] = kind
            elif kind.lower() not in (par or "").lower():
                data["parallel"] = ((par + " " + kind).strip() if par and par.lower() not in ("base","null","none") else kind)
                par = data["parallel"]
    auto_hit = bool(data.get("auto")) or re.search(r"\b(auto|autograph|on-card|sticker auto|signed)\b", blob)
    if auto_hit:
        data["auto"] = True
        mix = (par + " " + ins).lower()
        if "auto" not in mix and "autograph" not in mix:
            data["parallel"] = ((par + " Autograph").strip() if par and par.lower() not in ("base", "null", "none") else "Autograph")
            par = data["parallel"]
    if "splendor" in blob:
        if "splendor" not in st.lower():
            data["set"] = ((st + " Splendor").strip() if st and "upper deck" in st.lower() else "Splendor")
        if not re.search(r"/\s*\d+", par or ""):
            m = re.search(r"(\d{1,3}\s*/\s*\d{1,3}|/\s*\d{1,3})", str(data.get("number") or "") + " " + str(data.get("notes") or ""))
            if m:
                data["parallel"] = ((par + " " + m.group(0).replace(" ","")).strip() if par and par.lower() not in ("base","null","none") else m.group(0).replace(" ",""))
                par = data["parallel"]
    if "deluxe" in blob:
        if "young gun" in blob or " yg" in blob:
            if not ins or ins.lower() in ("base", "null", "none"):
                data["insert"] = "Young Guns"
        if "deluxe" not in (par or "").lower():
            run = "/250"
            m = re.search(r"/\s*(\d{1,3})", par or "") or re.search(r"/\s*(\d{1,3})", str(data.get("number") or ""))
            if m:
                run = "/" + m.group(1)
            data["parallel"] = ("Deluxe " + run).strip()
            par = data["parallel"]
        if not par or par.lower() in ("base", "null", "none", "young guns", "yg"):
            data["parallel"] = par if par and "/" in par else (data.get("parallel") or par)
    if "choice" in blob and "reserve" in blob:
        if "choice" not in st.lower():
            data["set"] = (st + " Choice").strip() if st else "Choice"
        if "reserve" not in (par + " " + ins).lower():
            data["parallel"] = ((par + " Reserve").strip() if par and par.lower() not in ("base",) else "Reserve")
    scrub_false_yg(data)
    if not data["hockey"]:
        data["needs_review"] = True
        data["blocked"] = True
        label = data.get("sport") or "not hockey"
        data["notes"] = (data.get("notes") or "") + " Not a hockey card. Clappers PC only catalogs pro hockey."
    try:
        con = db()
        ensure_catalog(con)
        data["matches"] = catalog_matches(con, data)
        con.close()
    except Exception:
        data["matches"] = []
    return data


COMP_MODEL = os.environ.get("COMP_MODEL", "grok-4-1-fast-non-reasoning")
COMP_PROMPT = """You may use AT MOST ONE web_search.
Search this exact string (do not drop player, year, set, card #, /print-run, or parallel):
{card}
Only look at public SOLD / completed lots on Fanatics Collect, Goldin Auctions, and Heritage Auctions.
Do not scrape eBay. Do not open 130point. Do not use asking prices or live bids.
From THAT ONE search, fill every grade you actually see for this same player / year / set / number / parallel. Prefer the newest sold date. Ignore a 2022 lot if a 2025–2026 sold exists. Leave a grade null if you did not see a sold of that grade. Do not guess. Do not copy raw into PSA 10 or PSA 9 into PSA 10.
Print run is /249 not 047/249.
Return ONLY JSON, no markdown:
{{
  "suggested_cad": number|null,
  "raw_cad": number|null,
  "psa6_cad": number|null,
  "psa7_cad": number|null,
  "psa8_cad": number|null,
  "psa9_cad": number|null,
  "psa10_cad": number|null,
  "bgs9_cad": number|null,
  "bgs95_cad": number|null,
  "bgs10_cad": number|null,
  "sgc10_cad": number|null,
  "low": number|null,
  "high": number|null,
  "currency": "CAD"|null,
  "sample_count": number,
  "needs_review": boolean,
  "summary": string,
  "sources": [string]
}}
suggested_cad is the sold that matches the scanned copy's grader/grade when present, else raw_cad. Convert USD at 1.35.
Never use the card number, year, print run, or cert as a price.
"""

def _order_grades(data: dict) -> dict:
    if not isinstance(data, dict):
        return data
    def climb(keys):
        floor = None
        for k in keys:
            try:
                v = float(data.get(k))
            except (TypeError, ValueError):
                continue
            if v < 1:
                continue
            if floor is not None and v < floor:
                data[k] = floor
            else:
                floor = v
    climb(("psa6_cad","psa7_cad","psa8_cad","psa9_cad","psa10_cad"))
    climb(("bgs9_cad","bgs95_cad","bgs10_cad","bgs_black_cad"))
    return data

def _usd_to_cad(n):
    try:
        return round(float(n) * 1.35, 2)
    except (TypeError, ValueError):
        return None

def _sale_cad(item: dict):
    try:
        n = float(item.get("price"))
    except (TypeError, ValueError):
        return None
    if n < 1 or n > 20000:
        return None
    cur = str(item.get("currency") or item.get("curr") or "USD").upper().replace("CDN", "CAD")
    if cur in ("CAD", "C$", "CAN"):
        return round(n, 2)
    return _usd_to_cad(n)

def _median(vals):
    vals = sorted(v for v in vals if v and v >= 1)
    if not vals:
        return None
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else round((vals[mid - 1] + vals[mid]) / 2, 2)

def _sale_bucket(item: dict) -> str | None:
    grader = str(item.get("grader") or "").upper().strip()
    grade = str(item.get("grade") or "").strip().lower()
    if grader in ("", "RAW", "UNGRADED", "NONE", "N/A", "NULL") and grade in ("", "raw", "ungraded", "none", "n/a"):
        return "raw_cad"
    if grader in ("PSA",):
        if grade.startswith("10"):
            return "psa10_cad"
        if "9.5" in grade:
            return "psa9_cad"
        if grade.startswith("9"):
            return "psa9_cad"
        if grade.startswith("8"):
            return "psa8_cad"
        if grade.startswith("7"):
            return "psa7_cad"
        if grade.startswith("6"):
            return "psa6_cad"
        return "raw_cad"
    if grader in ("BGS", "BECKETT", "BECKETT GRADING SERVICES"):
        if "black" in grade:
            return "bgs_black_cad"
        if "9.5" in grade:
            return "bgs95_cad"
        if grade.startswith("10") or "pristine" in grade or "found" in grade:
            return "bgs10_cad"
        if grade.startswith("9"):
            return "bgs9_cad"
        return "raw_cad"
    if grader in ("SGC",):
        if grade.startswith("10") or "10" in grade:
            return "sgc10_cad"
        return "raw_cad"
    if grader in ("KSA", "KIDDLEY'S", "KIDDLEY"):
        if "9.5" in grade or "95" in grade:
            return "ksa95_cad"
        if grade.startswith("10"):
            return "ksa10_cad"
        if grade.startswith("9"):
            return "ksa9_cad"
        return "raw_cad"
    return "raw_cad"

def _junk_title(title: str) -> bool:
    t = (title or "").lower()
    return bool(re.search(
        r"\b(lot|lots|lot of|\d+\s*card lot|bundle|collection|complete set|reprint|proxy|digital|nft|damaged|ripped|creased|wholesale|\d+x|x\d+|box break|team break|spot|shipping only|pwe|sticker only|code only|digital code|checklist)\b",
        t,
    ))

def _is_yg(card_or_q) -> bool:
    s = ""
    if isinstance(card_or_q, dict):
        s = " ".join(str(card_or_q.get(k) or "") for k in ("insert","set","parallel","player"))
    else:
        s = str(card_or_q or "")
    return bool(re.search(r"young guns|\byg\b", s.lower()))

def _cache_worthy(data: dict, card: dict | None = None) -> bool:
    if not isinstance(data, dict):
        return False
    raw = data.get("raw_cad")
    try:
        raw_n = float(data.get("raw_n") or 0)
    except (TypeError, ValueError):
        raw_n = 0
    try:
        raw_f = float(raw) if raw is not None else None
    except (TypeError, ValueError):
        raw_f = None
    keys = ("suggested_cad","raw_cad","psa6_cad","psa7_cad","psa8_cad","psa9_cad","psa10_cad","bgs9_cad","bgs95_cad","bgs10_cad","bgs_black_cad","sgc10_cad")
    return any(data.get(k) for k in keys)

def _drop_junk_raw(out: dict, q: str):
    return out

PAR_FLAGS = (
    "gold", "orange", "red", "blue", "green", "purple", "pink", "black", "silver",
    "rainbow", "ice", "canvas", "acetate", "outburst", "extravagance", "exclusive",
    "high gloss", "spectrum", "superfractor", "printing plate", "clear cut",
    "die-cut", "die cut", "holographic", "precious metal", "pmg", "jade", "emerald",
    "sapphire", "ruby", "bronze", "platinum", "yellow", "teal",
)

def _par_need(parallel: str) -> list:
    p = re.sub(r"\s+", " ", (parallel or "").strip().lower())
    if not p or p in ("base", "none", "n/a"):
        return []
    need = []
    run = re.search(r"/\s*(\d{1,4})", p)
    color = re.sub(r"/.*", "", p).strip()
    color = re.sub(r"[^a-z0-9 /]+", " ", color).strip()
    if color and color not in ("parallel", "color"):
        need.append(color)
    if run:
        need.append("/" + run.group(1))
    return need

def _phrase_in_title(phrase: str, t: str) -> bool:
    p = re.sub(r"\s+", " ", (phrase or "").strip().lower())
    if not p:
        return True
    if p in t:
        return True
    words = [w for w in p.split() if w and w not in ("the", "ud")]
    if len(words) >= 2 and all(re.search(rf"\b{re.escape(w)}\b", t) for w in words):
        return True
    if p == "exclusives" and "ud exclusives" in t:
        return True
    return False

SET_KEYS = (
    "young guns", "allure", "splendor", "premier", "artifacts", "the cup",
    "black diamond", "series 1", "series 2", "series 3", "extended",
    "sp authentic", "sp game used", "clear cut", "o-pee-chee", "opc platinum",
    "metal universe", "fleer ultra", "skybox", "starquest", "choice reserve",
    "canvas", "ice premieres",
)

SET_ALIASES = {
    "young guns": (r"young guns", r"\byg\b"),
    "series 1": (r"series\s*1", r"\bs1\b", r"\bser\.?\s*1\b", r"\bud\s*s1\b"),
    "series 2": (r"series\s*2", r"\bs2\b", r"\bser\.?\s*2\b", r"\bud\s*s2\b"),
    "series 3": (r"series\s*3", r"\bs3\b", r"\bser\.?\s*3\b", r"\bud\s*s3\b"),
    "extended": (r"extended", r"\bext\b"),
    "sp authentic": (r"sp authentic", r"\bspa\b", r"\bsp-?a\b", r"sp auth"),
    "sp game used": (r"sp game used", r"\bspgu\b", r"spgu"),
    "o-pee-chee": (r"o-pee-chee", r"\bopc\b", r"opeechee"),
    "the cup": (r"the cup", r"\bcup\b"),
    "ice premieres": (r"ice premieres", r"ice premiere"),
    "choice reserve": (r"choice reserve", r"reserve"),
}

def _set_in_title(key: str, t: str) -> bool:
    if key == "young guns" and re.search(r"young guns|\byg\b", t):
        return True
    if key in t:
        return True
    for rx in SET_ALIASES.get(key, ()):
        if re.search(rx, t, flags=re.I):
            return True
    return False

def _sale_fits(title: str, q: str, player: str, parallel: str = "", number: str = "", year: str = "") -> bool:
    t = (title or "").lower()
    parts = (player or "").strip().split()
    last = parts[-1].lower() if parts else ""
    first = parts[0].lower() if len(parts) > 1 else ""
    if last:
        alts = {last}
        if last.endswith("sky"):
            alts.add(last[:-1] + "i")
        if last.endswith("ski"):
            alts.add(last[:-1] + "y")
        if last.endswith("ov"):
            alts.add(last + "a")
        if not any(a in t for a in alts):
            return False
    if first and len(first) > 3 and first not in ("alex", "john", "mike", "chris", "matt", "nick"):
        if first not in t and last not in t:
            return False
    if _junk_title(title):
        return False
    required = (q or "").split(" -(")[0].lower()
    ql = required
    years_q = re.findall(r"\b((?:19|20)\d{2})\b", required)
    season = year or (years_q[0] if years_q else "")
    if season:
        titled = bool(re.search(r"\b(?:19|20)\d{2}\b", t) or re.search(r"\b\d{2}\s*[-/]\s*\d{2}\b", t))
        if titled and not _season_hit(t, season):
            return False
    if "young guns" in ql or " yg" in f" {ql}":
        if not re.search(r"young guns|\byg\b", t):
            return False
    elif re.search(r"young guns|\byg\b", t) and "young guns" not in (parallel or "").lower():
        return False
    if "renewed" in t and "renewed" not in ql:
        return False
    need = _par_need(parallel)
    if need:
        for n in need:
            if n.startswith("/"):
                if not re.search(r"/\s*" + re.escape(n[1:]) + r"\b", t):
                    return False
            elif not _phrase_in_title(n, t):
                return False
        if "exclusives" not in ql and re.search(r"exclusive", t) and "exclusives" not in " ".join(need):
            return False
    else:
        if re.search(r"/\s*(?:1|5|10|25|49|50|99|100|150|199|249|299|349|399|499|999)\b", t):
            return False
        extra = (
            "orange", "gold vinyl", "superfractor", "printing plate",
            "outburst", "extravagance", "canvas", "acetate", "clear cut",
            "red rainbow", "green parallel", "blue parallel", "pink",
            "future watch", "sizzle reel", "exclusives", "high gloss",
            "deluxe", "speckle", "holofoil",
        )
        if any(flag in t and flag not in ql for flag in extra):
            return False
        if re.search(r"\b(gold\s*/|gold\s+parallel|outburst\s+gold|gold\s+outburst|gold\s+vinyl)\b", t) and "gold" not in ql:
            return False
    num = re.sub(r"[^\d]", "", str(number or "").split("/")[0])
    if num and len(num) >= 3 and num not in t and re.search(r"#\s*\d+", t) and "young guns" not in ql:
        return False
    return True

def _code_num(num: str) -> str:
    n = re.sub(r"^#+", "", (num or "").strip())
    if re.search(r"[A-Za-z]", n) and re.search(r"\d", n):
        return n
    return ""


_GENERIC_PRODUCT = {
    "base", "young guns", "series 1", "series 2", "series 3",
    "extended", "upper deck", "hockey", "none", "n/a",
}


def _season_years(year: str) -> list:
    y = (year or "").strip()
    m = re.match(r"((?:19|20)\d{2})\s*[-/]\s*(\d{2,4})", y)
    if m:
        a = m.group(1)
        b = m.group(2)
        if len(b) == 2:
            b = a[:2] + b
        return [a, b]
    m = re.match(r"^(\d{2})\s*[-/]\s*(\d{2})$", y)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        cen = "19" if a >= 50 else "20"
        return [f"{cen}{a:02d}", f"{cen}{b:02d}"]
    if re.match(r"(?:19|20)\d{2}$", y[:4] or ""):
        a = int(y[:4])
        return [str(a - 1), str(a), str(a + 1)]
    return []


def _season_hit(title: str, year: str) -> bool:
    t = (title or "").lower()
    ys = _season_years(year)
    if not ys:
        return True
    if any(y in t for y in ys):
        return True
    for i, a in enumerate(ys):
        for b in ys[i + 1:]:
            s1, s2 = a[2:], b[2:]
            if re.search(rf"\b{s1}\s*[-/]\s*{s2}\b", t):
                return True
            if re.search(rf"\b{a}\s*[-/]\s*{s2}\b", t) or re.search(rf"\b{a}\s*[-/]\s*{b}\b", t):
                return True
    return False


def _season_q(year: str) -> str:
    ys = _season_years(year)
    if not ys:
        return (year or "")[:4]
    a, b = ys[0], ys[1] if len(ys) > 1 else str(int(ys[0]) + 1)
    if a == b:
        return a
    return f"({a},{b})"


def _q_token(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    return f'"{s}"' if " " in s else s


def search_queries(card: dict) -> list:
    """One boolean q the Card API expects: player + product + this parallel + not other parallels."""
    player = str(card.get("player") or "").strip()
    if not player:
        return []
    year = str(card.get("year") or "").strip()
    ins = str(card.get("insert") or "").strip()
    st = str(card.get("set") or "").strip()
    par = str(card.get("parallel") or "").strip()
    if par.lower() in ("base", "none", "n/a"):
        par = ""
    num = re.sub(r"^#+", "", str(card.get("number") or "").strip())
    blob = f"{ins} {st} {par}".lower()
    yg = bool(re.search(r"young guns|\byg\b", blob))
    unique = bool(ins) and ins.lower() not in _GENERIC_PRODUCT and not yg

    if yg:
        product = "Young Guns"
    elif unique and ins.lower() not in ("rookie", "rc", "base", "insert"):
        product = ins
    else:
        product = re.sub(r"^(upper deck|ud)\s+", "", st, flags=re.I).strip() or st

    parts = [player, _q_token(product)]
    color = re.sub(r"/.*", "", par).strip()
    run = re.search(r"/\s*(\d{1,4})", par)
    if color and color.lower() not in (product.lower(), "base", "parallel"):
        parts.append(_q_token(color))
    if run:
        parts.append("/" + run.group(1))
    code = _code_num(num)
    if code:
        parts.append(code)
    q = " ".join(x for x in parts if x)
    q += " -(lot,checklist,reprint,bundle)"
    return [q]

def _clean_bucket(vals):
    vals = [v for v in vals if v and v >= 1]
    if len(vals) >= 3:
        mid = _median(vals)
        if mid:
            vals = [v for v in vals if v >= mid * 0.4]
    return vals

def _sale_id(item: dict) -> str:
    sid = str(item.get("id") or item.get("listing_id") or item.get("listing_url") or "").strip()
    if sid:
        return sid[:180]
    blob = f"{item.get('title')}|{item.get('price')}|{item.get('sale_date') or item.get('sold_at')}"
    return hashlib.sha1(blob.encode("utf-8", "ignore")).hexdigest()

def _parse_day(raw) -> str:
    if raw is None or raw == "":
        return ""
    if isinstance(raw, dict):
        for k in ("date", "day", "sold", "value", "sale_date", "sold_at"):
            got = _parse_day(raw.get(k))
            if got:
                return got
        return ""
    if isinstance(raw, (int, float)):
        ts = float(raw)
        if ts > 1e12:
            ts = ts / 1000.0
        if ts > 1e9:
            return datetime.fromtimestamp(ts, tz=TZ).date().isoformat()
        return ""
    s = str(raw).strip()
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    m = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})", s)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if a > 12:
            return f"{y:04d}-{b:02d}-{a:02d}"
        return f"{y:04d}-{a:02d}-{b:02d}"
    m = re.search(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{1,2}),?\s+(\d{4})", s, re.I)
    if m:
        months = "jan feb mar apr may jun jul aug sep oct nov dec".split()
        mo = months.index(m.group(1)[:3].lower()) + 1
        return f"{int(m.group(3)):04d}-{mo:02d}-{int(m.group(2)):02d}"
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(TZ).date().isoformat()
    except Exception:
        return ""

def _sale_when(item: dict) -> str:
    if not isinstance(item, dict):
        return today_iso()
    for k in ("sale_date", "sold_at", "sold_date", "date", "soldAt", "closed_at", "end_time", "ended_at"):
        got = _parse_day(item.get(k))
        if got:
            return got
    for k, v in item.items():
        lk = str(k).lower()
        if "date" in lk or lk.endswith("_at") or "sold" in lk:
            got = _parse_day(v)
            if got:
                return got
    return today_iso()

def save_house_solds(fp: str, rows: list):
    if not fp:
        return
    con = db()
    now = time.time()
    con.execute("DELETE FROM house_solds WHERE fp=?", (fp,))
    for r in rows or []:
        try:
            con.execute(
                "INSERT OR IGNORE INTO house_solds(id,fp,bucket,cad,sale_date,title,t) VALUES(?,?,?,?,?,?,?)",
                (r["id"], fp, r["bucket"], float(r["cad"]), r.get("date") or "", (r.get("title") or "")[:240], now),
            )
        except Exception:
            pass
    con.commit()
    con.close()

def series_from_sales(sales: list) -> dict:
    by = {}
    for s in sales or []:
        d = (s.get("date") or "")[:10]
        if len(d) < 10 or d[4] != "-":
            continue
        by.setdefault(s.get("bucket") or "raw_cad", {}).setdefault(d, []).append(float(s["cad"]))
    out = {}
    for bucket, days in by.items():
        pts = []
        for d in sorted(days):
            mid = _median(days[d])
            if mid:
                pts.append({"d": d, "v": mid})
        if pts:
            out[bucket] = pts
    return out

def merge_series(*blobs) -> dict:
    acc = {}
    for blob in blobs:
        for k, pts in (blob or {}).items():
            m = {p["d"]: p["v"] for p in (acc.get(k) or [])}
            for p in pts or []:
                if p and p.get("d") and p.get("v") is not None:
                    m[str(p["d"])[:10]] = float(p["v"])
            acc[k] = [{"d": d, "v": m[d]} for d in sorted(m)]
    return acc

def house_series(fp: str) -> dict:
    """Daily median close per grade from stored solds."""
    out = {}
    if not fp:
        return out
    by = {}
    try:
        con = db()
        rows = con.execute("SELECT bucket, cad, sale_date FROM house_solds WHERE fp=?", (fp,)).fetchall()
        con.close()
    except Exception:
        return out
    for r in rows:
        try:
            cad = float(r["cad"])
        except (TypeError, ValueError):
            continue
        d = (r["sale_date"] or "")[:10]
        if len(d) < 10 or d[4] != "-":
            continue
        by.setdefault(r["bucket"], {}).setdefault(d, []).append(cad)
    for bucket, days in by.items():
        series = []
        for d in sorted(days):
            vals = days[d]
            mid = _median(vals)
            if not mid:
                continue
            series.append({"d": d, "v": mid})
        if series:
            out[bucket] = series
    return out

def house_day_close(fp: str):
    """Median of each grade on the newest day that grade traded."""
    out, counts, when = {}, {}, {}
    if not fp:
        return out, counts, when
    today = datetime.now(timezone.utc).date().isoformat()
    by = {}
    try:
        con = db()
        rows = con.execute("SELECT bucket, cad, sale_date FROM house_solds WHERE fp=?", (fp,)).fetchall()
        con.close()
    except Exception:
        return out, counts, when
    for r in rows:
        try:
            cad = float(r["cad"])
        except (TypeError, ValueError):
            continue
        d = (r["sale_date"] or "")[:10] or today
        by.setdefault(r["bucket"], {}).setdefault(d, []).append(cad)
    for bucket, days in by.items():
        all_vals = [v for vals in days.values() for v in vals]
        mid14 = _median(_clean_bucket(all_vals) or all_vals)
        use = None
        for d in sorted(days.keys(), reverse=True):
            vals = days[d]
            mid = _median(_clean_bucket(vals) or vals)
            if not mid:
                continue
            if mid14 and mid < mid14 * 0.4 and len(vals) <= 2:
                continue
            use = d
            break
        if not use:
            dated = sorted(days.keys())
            use = dated[-1] if dated else None
        if not use:
            continue
        vals = days[use]
        mid = _median(_clean_bucket(vals) or vals)
        if mid:
            out[bucket] = mid
            counts[bucket] = len(vals)
            when[bucket] = use
    return out, counts, when

def load_house_solds(fp: str) -> dict:
    buckets = {}
    if not fp:
        return buckets
    try:
        con = db()
        for r in con.execute("SELECT bucket, cad FROM house_solds WHERE fp=?", (fp,)):
            try:
                buckets.setdefault(r["bucket"], []).append(float(r["cad"]))
            except (TypeError, ValueError):
                pass
        con.close()
    except Exception:
        return {}
    return buckets

def _ingest(rows, q, player, buckets, only_raw=None, sales=None, parallel="", number="", year=""):
    for item in rows or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "")
        if not _sale_fits(title, q, player, parallel, number, year):
            continue
        cad = _sale_cad(item)
        if not cad or cad < 1:
            continue
        key = _sale_bucket(item)
        if only_raw is True:
            if key and key != "raw_cad":
                continue
            key = "raw_cad"
        elif only_raw is False:
            if not key or key == "raw_cad":
                continue
        elif not key:
            continue
        buckets.setdefault(key, []).append(cad)
        if sales is not None:
            sales.append({
                "id": _sale_id(item),
                "bucket": key,
                "cad": cad,
                "date": _sale_when(item),
                "title": title,
            })

async def _card_api_rows(client, q: str, extra: dict) -> list:
    start = (datetime.now(timezone.utc) - timedelta(days=14)).date().isoformat()
    params = {"q": q, "limit": 100, "date_from": start}
    params.update(extra or {})
    rows = []
    cursor = None
    for _ in range(3):
        if cursor:
            params["cursor"] = cursor
        r = await client.get(
            "https://www.thecardapi.com/api/v1/market/sales",
            headers={"x-market-api-key": CARD_API_KEY},
            params=params,
        )
        if r.status_code >= 400:
            break
        body = r.json() if r.content else {}
        chunk = body.get("data") if isinstance(body, dict) else body
        if isinstance(chunk, list):
            rows.extend(chunk)
        pag = body.get("pagination") if isinstance(body, dict) else {}
        cursor = (pag or {}).get("next_cursor")
        if not cursor or not (pag or {}).get("has_more"):
            break
    return rows

async def fetch_card_api(q: str, player: str, fp: str = "", parallel: str = "", number: str = "", grader: str = "", grade: str = "", year: str = "") -> dict | None:
    if not CARD_API_KEY or not q:
        return None
    buckets = {}
    sales = []
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            mixed = await _card_api_rows(client, q, {})
            if not mixed and player:
                mixed = await _card_api_rows(client, player, {})
            raw_rows = [x for x in mixed if isinstance(x, dict) and not str(x.get("grader") or "").strip()]
            slab_rows = [x for x in mixed if isinstance(x, dict) and str(x.get("grader") or "").strip()]
            extra = {}
            g = (grader or "").strip().upper()
            gr = (grade or "").strip()
            if g and g != "RAW" and gr:
                extra = {"graded": True, "grader": g, "grade": gr.split()[0]}
            slab_exact = await _card_api_rows(client, q, extra) if extra else []
            _ingest(raw_rows or mixed, q, player, buckets, only_raw=True, sales=sales, parallel=parallel, number=number, year=year)
            _ingest(slab_rows, q, player, buckets, only_raw=False, sales=sales, parallel=parallel, number=number, year=year)
            _ingest(slab_exact, q, player, buckets, only_raw=False, sales=sales, parallel=parallel, number=number, year=year)
            _ingest(mixed, q, player, buckets, only_raw=None, sales=sales, parallel=parallel, number=number, year=year)
    except Exception as e:
        return {"query": q, "error": str(e)[:180], "sample_count": 0, "summary": "Card API error: "+str(e)[:120]}
    if fp and sales:
        save_house_solds(fp, sales)
        out, counts, when = house_day_close(fp)
        if not out:
            counts = {k: len(v) for k, v in buckets.items()}
            out = {k: _median(_clean_bucket(v) or v) for k, v in buckets.items()}
            out = {k: v for k, v in out.items() if v}
            when = {k: datetime.now(timezone.utc).date().isoformat() for k in out}
    else:
        counts = {k: len(v) for k, v in buckets.items()}
        out = {k: _median(_clean_bucket(v) or v) for k, v in buckets.items()}
        out = {k: v for k, v in out.items() if v}
        when = {}
    if not out:
        return None
    raw = out.get("raw_cad")
    if not out:
        return None
    out = _order_grades(out)
    out["raw_n"] = counts.get("raw_cad", 0)
    out["currency"] = "CAD"
    out["sample_count"] = sum(counts.values())
    out["close_day"] = when.get("raw_cad") or (max(when.values()) if when else "")
    out["hist_days"] = merge_series(house_series(fp) if fp else {}, series_from_sales(sales))
    out["house"] = True
    out["sources"] = ["thecardapi", "house"]
    tape = series_from_sales(sales).get("raw_cad") or series_from_sales(sales).get("psa10_cad") or []
    bits = [f"{p['d'][5:]} ${p['v']:.0f}" for p in tape[-6:]]
    out["summary"] = (
        f"Day close · {out.get('close_day') or 'today'} · "
        + (str(out["sample_count"])+" solds")
        + ((" · tape " + ", ".join(bits)) if bits else " · no dated solds")
    )
    out["query"] = q
    out["model"] = "card-api"
    return out

def book_job_on() -> bool:
    try:
        con = db()
        con.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
        row = con.execute("SELECT v FROM kv WHERE k='book_job'").fetchone()
        con.close()
        if not row:
            return False
        return (time.time() - float(row["v"])) < 45 * 60
    except Exception:
        return False

def pack_house(fp: str) -> dict | None:
    if not fp:
        return None
    out, counts, when = house_day_close(fp)
    series = house_series(fp)
    if not out and series:
        out, counts, when = {}, {}, {}
        for k, pts in series.items():
            if pts:
                out[k] = pts[-1]["v"]
                counts[k] = 1
                when[k] = pts[-1]["d"]
    if not out:
        return None
    out = _order_grades(out)
    out["raw_n"] = counts.get("raw_cad", 0)
    out["currency"] = "CAD"
    out["sample_count"] = sum(counts.values())
    out["close_day"] = when.get("raw_cad") or (max(when.values()) if when else "")
    out["hist_days"] = series
    out["house"] = True
    out["cached"] = True
    out["sources"] = ["house"]
    out["summary"] = f"House close · {out.get('close_day') or 'today'}."
    out["model"] = "house"
    return out

def _copy_bucket(card: dict) -> str:
    g = str(card.get("grader") or "Raw").upper()
    gr = str(card.get("grade") or "").strip()
    if g == "PSA" and gr.startswith("10"):
        return "psa10_cad"
    if g == "PSA" and gr.startswith("9"):
        return "psa9_cad"
    if g == "PSA" and gr.startswith("8"):
        return "psa8_cad"
    if g == "PSA" and gr.startswith("7"):
        return "psa7_cad"
    if g == "PSA" and gr.startswith("6"):
        return "psa6_cad"
    if g == "BGS" and "black" in gr.lower():
        return "bgs_black_cad"
    if g == "BGS" and "9.5" in gr:
        return "bgs95_cad"
    if g == "BGS" and gr.startswith("10"):
        return "bgs10_cad"
    if g == "BGS" and gr.startswith("9"):
        return "bgs9_cad"
    if g == "SGC" and gr.startswith("10"):
        return "sgc10_cad"
    if g == "KSA" and "9.5" in gr:
        return "ksa95_cad"
    if g == "KSA" and gr.startswith("10"):
        return "ksa10_cad"
    if g == "KSA" and gr.startswith("9"):
        return "ksa9_cad"
    return "raw_cad"

def expand_hist(pts: list, days: int = 14) -> list:
    end = now_toronto().date()
    start = end - timedelta(days=days - 1)
    by = {}
    for p in pts or []:
        d = str((p or {}).get("d") or "")[:10]
        try:
            v = float(p.get("v"))
        except (TypeError, ValueError):
            continue
        if len(d) == 10 and v >= 1:
            by[d] = v
    last = None
    for d in sorted(by):
        if d <= start.isoformat():
            last = by[d]
    out = []
    cur = start
    while cur <= end:
        key = cur.isoformat()
        if key in by:
            last = by[key]
        if last is not None:
            out.append({"d": key, "v": last})
        cur += timedelta(days=1)
    return out


def attach_hist_copy(data: dict, card: dict) -> dict:
    days = (data or {}).get("hist_days") or {}
    pts = days.get(_copy_bucket(card)) or days.get("raw_cad") or []
    val = None
    for k in ("suggested_cad", "raw_cad", "psa10_cad", "psa9_cad"):
        try:
            if data.get(k) and float(data.get(k)) >= 1:
                val = float(data.get(k))
                break
        except (TypeError, ValueError):
            pass
    if not pts and val:
        pts = [{"d": datetime.now(timezone.utc).date().isoformat(), "v": val}]
    data["hist_copy"] = expand_hist(pts, 14)
    return data

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
    if not XAI_API_KEY and not CARD_API_KEY:
        raise HTTPException(500, "No comps source set")
    check_cap()
    card = {k: payload.get(k) for k in ("player","year","set","number","parallel","insert","team","grader","grade","cert")}
    scrub_false_yg(card)
    ck = card_fp(card)
    day = time.strftime("%Y-%m-%d")
    ck_day = f"{ck}|{day}"
    nightly = bool(payload.get("auto") or payload.get("skip_web") or payload.get("force"))
    force = bool(payload.get("force"))
    pl = str(card.get("player") or "").strip().lower()
    yr = str(card.get("year") or "").strip().lower()
    if force and pl:
        try:
            con = db()
            like = pl + "|" + (yr + "|" if yr else "")
            con.execute("DELETE FROM comp_cache WHERE k LIKE ?", (like + "%",))
            con.commit()
            con.close()
        except Exception:
            pass
    if ck.strip("|") and not nightly:
        house = pack_house(ck)
        if house:
            return attach_hist_copy(house, card)
        con = db()
        row = con.execute("SELECT data, t FROM comp_cache WHERE k=?", (ck_day,)).fetchone()
        if not row:
            row = con.execute("SELECT data, t FROM comp_cache WHERE k=?", (ck,)).fetchone()
        con.close()
        if row and (time.time() - float(row["t"])) < 36 * 3600:
            try:
                cached = json.loads(row["data"])
                if isinstance(cached, dict) and any(cached.get(k) for k in ("raw_cad","psa10_cad","suggested_cad","psa9_cad")):
                    cached["cached"] = True
                    return attach_hist_copy(cached, card)
            except Exception:
                pass
    def run_only(v):
        s = re.sub(r"\b\d+\s*/\s*(\d+)\b", r"/\1", str(v or ""))
        s = re.sub(r"\b\d+\s+of\s+(\d+)\b", r"/\1", s, flags=re.I)
        return s
    num = str(card.get("number") or "").strip()
    run = ""
    m = re.search(r"(\d+)\s*/\s*(\d+)", num)
    if m:
        run = "/" + m.group(2)
        num = re.sub(r"\s*\d+\s*/\s*\d+\s*", " ", num).strip()
    par = run_only(card.get("parallel"))
    ins = run_only(card.get("insert"))
    qs = search_queries(card)
    q = qs[0] if qs else " ".join(str(x) for x in [card.get("player"), ins or card.get("set"), card.get("year")] if x)
    api_hit = None
    for qtry in qs:
        extra = await fetch_card_api(qtry, card.get("player") or "", ck, card.get("parallel") or "", card.get("number") or "", card.get("grader") or "", card.get("grade") or "", card.get("year") or "")
        if extra:
            api_hit = extra
            if extra.get("raw_cad") or extra.get("psa10_cad") or extra.get("sample_count"):
                break
    if ck.strip("|") and not force:
        house = pack_house(ck)
        if house:
            api_hit = house
    text = ""
    used = COMP_MODEL
    err = None
    data = {}
    skip_web = bool(payload.get("skip_web") or payload.get("auto"))
    if api_hit:
        data = api_hit
        used = "card-api"
        data = attach_hist_copy(data, card)
    elif XAI_API_KEY and not skip_web:
        label = q + " sold Fanatics Collect OR Goldin OR Heritage"
        headers = {"Authorization": f"Bearer {XAI_API_KEY}", "Content-Type": "application/json"}
        prompt = COMP_PROMPT.format(card=label)
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
            "psa6": "psa6_cad", "psa 6": "psa6_cad",
            "psa7": "psa7_cad", "psa 7": "psa7_cad",
            "psa8": "psa8_cad", "psa 8": "psa8_cad",
            "psa9": "psa9_cad", "psa 9": "psa9_cad",
            "psa10": "psa10_cad", "psa 10": "psa10_cad",
            "bgs9": "bgs9_cad", "bgs 9": "bgs9_cad",
            "bgs95": "bgs95_cad", "bgs 9.5": "bgs95_cad",
            "bgs10": "bgs10_cad", "bgs 10": "bgs10_cad", "found 10": "bgs10_cad", "pristine": "bgs10_cad",
            "bgs black": "bgs_black_cad", "black label": "bgs_black_cad", "bgs black label": "bgs_black_cad",
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
        (r"PSA\s*7(?:\.0)?[^0-9.]{0,12}(?:CAD|USD|C\$|US\$|\$)\s*([0-9]{1,5}(?:\.[0-9]{1,2})?)", "psa7_cad"),
        (r"PSA\s*6(?:\.0)?[^0-9.]{0,12}(?:CAD|USD|C\$|US\$|\$)\s*([0-9]{1,5}(?:\.[0-9]{1,2})?)", "psa6_cad"),
        (r"BGS\s*9\.5[^0-9]{0,12}(?:CAD|USD|C\$|US\$|\$)\s*([0-9]{1,5}(?:\.[0-9]{1,2})?)", "bgs95_cad"),
        (r"BGS\s*9(?:\.0)?[^0-9.]{0,12}(?:CAD|USD|C\$|US\$|\$)\s*([0-9]{1,5}(?:\.[0-9]{1,2})?)", "bgs9_cad"),
        (r"BGS\s*10[^0-9]{0,12}(?:CAD|USD|C\$|US\$|\$)\s*([0-9]{1,5}(?:\.[0-9]{1,2})?)", "bgs10_cad"),
        (r"(?:BGS\s*)?Black\s*Label[^0-9]{0,12}(?:CAD|USD|C\$|US\$|\$)\s*([0-9]{1,5}(?:\.[0-9]{1,2})?)", "bgs_black_cad"),
        (r"(?:Found|Pristine)\s*10[^0-9]{0,12}(?:CAD|USD|C\$|US\$|\$)\s*([0-9]{1,5}(?:\.[0-9]{1,2})?)", "bgs10_cad"),
        (r"SGC\s*10[^0-9]{0,12}(?:CAD|USD|C\$|US\$|\$)\s*([0-9]{1,5}(?:\.[0-9]{1,2})?)", "sgc10_cad"),
        (r"(?:raw|ungraded)[^0-9]{0,16}(?:CAD|USD|C\$|US\$|\$)\s*([0-9]{1,5}(?:\.[0-9]{1,2})?)", "raw_cad"),
    ):
        if data.get(dest) is None:
            m = re.search(rx, text_l, flags=re.I)
            if m:
                data[dest] = m.group(1)

    for key in ("suggested_cad", "suggested_usd", "raw_cad", "psa6_cad", "psa7_cad", "psa8_cad", "psa9_cad", "psa10_cad", "bgs9_cad", "bgs95_cad", "bgs10_cad", "bgs_black_cad", "sgc10_cad", "low", "high"):
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
    if data.get("suggested_cad") is None and data.get("suggested_usd") is None:
        data["needs_review"] = True
        data["sample_count"] = data.get("sample_count") or 0
        data["summary"] = data.get("summary") or "No Fanatics / Goldin / Heritage sold matched this copy."
    money_keys = ("suggested_cad","suggested_usd","raw_cad","psa6_cad","psa7_cad","psa8_cad","psa9_cad","psa10_cad","bgs9_cad","bgs95_cad","bgs10_cad","bgs_black_cad","sgc10_cad")
    if not any(data.get(k) for k in money_keys) and ck.strip("|") and not payload.get("auto") and not payload.get("force"):
        try:
            con = db()
            row = con.execute("SELECT data FROM comp_cache WHERE k=?", (ck,)).fetchone()
            con.close()
            if row:
                old = json.loads(row["data"])
                if isinstance(old, dict) and any(old.get(k) for k in money_keys):
                    old["cached"] = True
                    old["summary"] = (old.get("summary") or "") + " Kept last sold; new search was empty."
                    return attach_hist_copy(old, card)
        except Exception:
            pass
    if data.get("suggested_cad") is None:
        g = str(card.get("grader") or "Raw").upper()
        gr = str(card.get("grade") or "")
        pick = None
        if g in ("", "RAW"):
            pick = data.get("raw_cad")
        elif g == "PSA" and gr.startswith("10"):
            pick = data.get("psa10_cad")
        elif g == "PSA" and gr.startswith("9"):
            pick = data.get("psa9_cad")
        elif g == "PSA" and gr.startswith("8"):
            pick = data.get("psa8_cad")
        elif g == "BGS" and re.search(r"black", gr):
            pick = data.get("bgs_black_cad")
        elif g == "BGS" and "9.5" in gr:
            pick = data.get("bgs95_cad")
        elif g == "SGC" and gr.startswith("10"):
            pick = data.get("sgc10_cad")
        else:
            pick = data.get("raw_cad")
        if pick is not None:
            data["suggested_cad"] = pick
    if not any(data.get(k) for k in money_keys):
        prior = pack_house(ck) if ck.strip("|") else None
        if prior and any(prior.get(k) for k in money_keys):
            data = prior
            data["cached"] = True
            data["summary"] = (data.get("summary") or "") + " Kept last baseline; no solds in this 14-day window."
        else:
            data["raw_cad"] = 1
            data["suggested_cad"] = 1
            data["sample_count"] = data.get("sample_count") or 0
            data["summary"] = data.get("summary") or "No solds in the lookback; $1 floor."
            data["floor"] = True
    data = _order_grades(data)
    data["model"] = used
    data["card"] = card
    if err and not data.get("suggested_cad") and not data.get("suggested_usd"):
        data["error"] = err
        data["needs_review"] = True
        data["summary"] = data.get("summary") or "Could not read solds automatically."
    if ck.strip("|"):
        data = _drop_junk_raw(data, q + " " + (card.get("insert") or ""))
        data = attach_hist_copy(data, card)
        try:
            con = db()
            con.execute(
                "INSERT OR REPLACE INTO comp_cache(k,data,t) VALUES(?,?,?)",
                (ck_day, json.dumps(data), time.time()),
            )
            con.commit()
            con.close()
            spread_comp(ck, data)
        except Exception:
            pass
    return data

def _card_fp(c: dict) -> str:
    return card_fp(c)

def _copy_sold(c: dict, data: dict):
    g = str(c.get("grader") or "Raw").upper()
    gr = str(c.get("grade") or "").strip()
    if g in ("", "RAW") or not gr:
        return data.get("raw_cad")
    if g == "PSA" and gr.startswith("10"):
        return data.get("psa10_cad")
    if g == "PSA" and gr.startswith("9"):
        return data.get("psa9_cad")
    if g == "PSA" and gr.startswith("8"):
        return data.get("psa8_cad")
    if g == "PSA" and gr.startswith("7"):
        return data.get("psa7_cad")
    if g == "PSA" and gr.startswith("6"):
        return data.get("psa6_cad")
    if g == "BGS" and "black" in gr:
        return data.get("bgs_black_cad")
    if g == "BGS" and "9.5" in gr:
        return data.get("bgs95_cad")
    if g == "BGS" and gr.startswith("9"):
        return data.get("bgs9_cad")
    if g == "BGS" and gr.startswith("10"):
        return data.get("bgs10_cad")
    if g == "SGC" and gr.startswith("10"):
        return data.get("sgc10_cad")
    return None

def spread_comp(ck: str, data: dict):
    """Push latest house solds onto every saved copy of this card."""
    con = db()
    rows = con.execute("SELECT id, data FROM cards").fetchall()
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    fields = (
        ("raw_cad", "rawComp", "Raw"),
        ("psa6_cad", "psa6Comp", "PSA 6"),
        ("psa7_cad", "psa7Comp", "PSA 7"),
        ("psa8_cad", "psa8Comp", "PSA 8"),
        ("psa9_cad", "psa9Comp", "PSA 9"),
        ("psa10_cad", "psa10Comp", "PSA 10"),
        ("bgs9_cad", "bgs9Comp", "BGS 9"),
        ("bgs95_cad", "bgs95Comp", "BGS 9.5"),
        ("bgs10_cad", "bgs10Comp", "BGS 10"),
        ("bgs_black_cad", "bgsBlackComp", "BGS Black"),
        ("sgc10_cad", "sgc10Comp", "SGC 10"),
    )
    for r in rows:
        try:
            c = json.loads(r["data"])
        except Exception:
            continue
        if _card_fp(c) != ck:
            continue
        book = c.get("book") if isinstance(c.get("book"), dict) else {}
        for src, field, label in fields:
            try:
                v = float(data.get(src))
            except (TypeError, ValueError):
                continue
            if v < 1:
                continue
            c[field] = v
            book[label] = v
        copy = _copy_sold(c, data)
        try:
            copy = float(copy) if copy is not None else None
        except (TypeError, ValueError):
            copy = None
        if copy is not None and copy >= 1:
            oldc = None
            try:
                oldc = float(c.get("comp"))
            except (TypeError, ValueError):
                oldc = None
            if not (oldc is not None and copy is None):
                c["sysComp"] = copy
                c["comp"] = copy
                c["compAt"] = now
        c["book"] = book
        con.execute("UPDATE cards SET data=? WHERE id=?", (json.dumps(c), r["id"]))
    con.commit()
    con.close()


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
    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
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
    CREATE TABLE IF NOT EXISTS comp_cache (
      k TEXT PRIMARY KEY,
      data TEXT NOT NULL,
      t REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS house_solds (
      id TEXT PRIMARY KEY,
      fp TEXT NOT NULL,
      bucket TEXT NOT NULL,
      cad REAL NOT NULL,
      sale_date TEXT,
      title TEXT,
      t REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS house_solds_fp ON house_solds(fp);
    """)
    try:
        con.execute("ALTER TABLE users ADD COLUMN plan TEXT DEFAULT 'free'")
    except sqlite3.OperationalError:
        pass
    try:
        con.execute("ALTER TABLE users ADD COLUMN slug TEXT")
    except sqlite3.OperationalError:
        pass
    for col, spec in (("display", "TEXT"), ("hue", "TEXT"), ("bio", "TEXT"), ("avatar", "TEXT"), ("cropx", "TEXT"), ("cropy", "TEXT"), ("cropz", "TEXT"), ("avatar_hidden", "INTEGER NOT NULL DEFAULT 0"), ("credits", "INTEGER NOT NULL DEFAULT 0"), ("credit_month", "TEXT"), ("credit_until", "TEXT"), ("plus_until", "TEXT"), ("cycle_start", "TEXT"), ("suspended", "INTEGER NOT NULL DEFAULT 0"), ("socials", "TEXT"), ("last_book_auto", "TEXT"), ("book_hist", "TEXT")):
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
    ensure_catalog(con)
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
    for m in re.finditer(r"(?<![0-9])/\s*(\d{1,3})(?![0-9])", blob):
        if 1 <= int(m.group(1)) <= 25:
            return True
    for m in re.finditer(r"\b(\d{1,3})\s*/\s*(\d{1,3})\b", blob):
        if 1 <= int(m.group(2)) <= 25:
            return True
    if re.search(r"\bof\s*(?:[1-9]|1[0-9]|2[0-5])\b", blob):
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
        "has_photo": bool((c.get("photo") or c.get("scan") or "") and len(str(c.get("photo") or c.get("scan") or "")) > 80) or bool(c.get("has_photo")),
        # never send what they paid to other collectors
        "comp": card_market(c) or c.get("comp"),
        "sysComp": card_market(c),
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
        (user_id, slug or "", body[:500], time.strftime("%Y-%m-%dT%H:%M:%SZ"), card_id or ""),
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

def now_toronto():
    return datetime.now(TZ)

def today_iso():
    return now_toronto().date().isoformat()

def season_key(year: str) -> str:
    y = (year or "").strip()
    if re.match(r"(?:19|20)\d{2}$", y[:4] or ""):
        a = y[:4]
        return f"{a}-{str(int(a)+1)[2:]}"
    ys = _season_years(y)
    if len(ys) >= 2:
        return f"{ys[0]}-{ys[1][2:]}"
    return y.lower()

def card_fp(card: dict) -> str:
    return "|".join([
        str(card.get("player") or "").strip().lower(),
        season_key(card.get("year") or ""),
        str(card.get("set") or "").strip().lower(),
        str(card.get("number") or "").strip().lower(),
        str(card.get("parallel") or "").strip().lower(),
        str(card.get("insert") or "").strip().lower(),
    ])

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
    out = {
        "used": used, "cap": cap, "bonus": bonus,
        "left": monthly_left + bonus,
        "book_used": bused, "book_cap": bcap, "book_left": max(0, bcap - bused),
        "plan": plan, "month": m,
        "reset_at": reset_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rip_scans": RIP_SCANS, "rip_price": RIP_PRICE,
        "operator": is_operator(uid),
    }
    if is_operator(uid):
        out["book_cap"] = 9999
        out["book_left"] = 9999
    return out

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

def toronto_day() -> str:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Toronto")).strftime("%Y-%m-%d")
    except Exception:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

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
    uid = con.execute("SELECT id FROM users WHERE lower(email)=?", (email,)).fetchone()["id"]
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
    row = con.execute("SELECT id,pw,suspended FROM users WHERE lower(email)=?", (email,)).fetchone()
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
    try:
        scrub_user_yg(uid)
    except Exception:
        pass
    payload = dict(payload or {})
    scrub_false_yg(payload)
    payload["auto"] = True
    payload["force"] = is_operator(uid) or plan_of(uid) == "plus"
    payload["skip_web"] = True
    return await comp(payload, payload.get("secret"), x_token)

@app.post("/book-job")
async def book_job(payload: dict, request: Request, x_token: str | None = Header(default=None)):
    require_user(request, x_token)
    on = bool(payload.get("on"))
    con = db()
    con.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
    if on:
        con.execute("INSERT OR REPLACE INTO kv(k,v) VALUES('book_job',?)", (str(time.time()),))
    else:
        con.execute("DELETE FROM kv WHERE k='book_job'")
    con.commit()
    con.close()
    return {"on": on}

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
    row = con.execute("SELECT id FROM users WHERE lower(email)=?", (email,)).fetchone()
    if not row:
        con.close()
        raise HTTPException(404, "no account with that email")
    uid = row["id"]
    con.close()
    add_credits(uid, scans)
    send_mail("Clappers PC Rip Night", f"Granted {scans} extra IDs to {email}")
    return {"ok": True, "email": email, "scans": scans}

@app.post("/reset")
async def reset_request(payload: dict):
    email = (payload.get("email") or "").strip().lower()
    if "@" not in email:
        raise HTTPException(400, "enter the account email")
    con = db()
    row = con.execute("SELECT id FROM users WHERE lower(email)=?", (email,)).fetchone()
    if row:
        token = secrets.token_urlsafe(8).lower()
        con.execute("DELETE FROM resets WHERE email=?", (email,))
        con.execute("INSERT INTO resets(email,token,created) VALUES(?,?,?)", (email, token, int(time.time())))
        con.commit()
        sent = send_mail(
            "Clappers PC password reset",
            f"Your Clappers PC reset code is: {token}\n\nIt expires in 30 minutes. If you didn't ask for this, ignore the email.",
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
    ok = send_mail("Clappers PC mail test", "If you got this, mail is working on Clappers PC.", to=to)
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
    user = con.execute("SELECT id FROM users WHERE lower(email)=?", (email,)).fetchone()
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
    scrub_user_yg(uid)
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
        pc["has_photo"] = photo_file_exists(uid, pc.get("id") or "") or pc.get("has_photo")
        out.append(pc)
    return {"cards": out}

@app.get("/me/cards/{cid}/photo")
async def my_card_photo(cid: str, request: Request, x_token: str | None = Header(default=None)):
    tok = x_token or request.query_params.get("token")
    uid = require_user(request, tok)
    slug = ensure_slug(uid)
    return await card_photo(slug, cid)

@app.post("/cards")
async def upsert_card(payload: dict, request: Request, x_token: str | None = Header(default=None)):
    uid = require_user(request, x_token)
    card = payload.get("card") or payload
    cid = card.get("id") or ("c" + str(int(time.time()*1000)))
    card["id"] = cid
    blob = card.get("photo") or card.get("scan") or ""
    if save_card_photo(uid, cid, blob):
        card["has_photo"] = True
        card["photo"] = ""
        card["scan"] = ""
    elif photo_file_exists(uid, cid):
        card["has_photo"] = True
        card["photo"] = ""
        card["scan"] = ""
    else:
        for k in ("scan", "photo"):
            v = card.get(k)
            if isinstance(v, str) and len(v) > 1800000:
                card[k] = v[:1800000]
    con = db()
    existed = con.execute("SELECT id, data FROM cards WHERE id=? AND user_id=?", (cid, uid)).fetchone()
    old = {}
    if existed:
        try:
            old = json.loads(existed["data"])
        except Exception:
            old = {}
    else:
        fp = _card_fp(card)
        if fp.strip("|"):
            for r in con.execute("SELECT data FROM cards WHERE user_id=?", (uid,)).fetchall():
                try:
                    o = json.loads(r["data"])
                except Exception:
                    continue
                if _card_fp(o) == fp:
                    old = o
                    break
    keep = ("comp","sysComp","rawComp","psa6Comp","psa7Comp","psa8Comp","psa9Comp","psa10Comp","bgs9Comp","bgs95Comp","bgs10Comp","bgsBlackComp","sgc10Comp","book","compAt")
    def _alive(v):
        try:
            return v is not None and v != "" and float(v) >= 1
        except (TypeError, ValueError):
            return bool(v)
    if old:
        for k in keep:
            if k == "book":
                book = old.get("book") if isinstance(old.get("book"), dict) else {}
                incoming = card.get("book") if isinstance(card.get("book"), dict) else {}
                merged = dict(book)
                for bk, bv in incoming.items():
                    if _alive(bv):
                        merged[bk] = bv
                if incoming.get("suggested_cad") == 1 or incoming.get("raw_cad") == 1 or card.get("comp") == 1:
                    merged = incoming or {"raw_cad": 1, "suggested_cad": 1}
                card["book"] = merged
                continue
            if card.get("comp") == 1 or card.get("rawComp") == 1 or card.get("floor"):
                continue
            if not _alive(card.get(k)) and _alive(old.get(k)):
                card[k] = old[k]
    con.execute(
        "INSERT OR REPLACE INTO cards(id,user_id,data) VALUES(?,?,?)",
        (cid, uid, json.dumps(card)),
    )
    try:
        vote_card(con, uid, card)
    except Exception:
        pass
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
    try:
        photo_path(uid, cid).unlink(missing_ok=True)
    except Exception:
        pass
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
                "sysComp": card_market(raw),
                "rawComp": raw.get("rawComp"),
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
    raw = {}
    if row:
        try:
            raw = json.loads(row["data"])
        except Exception:
            raw = {}
    got = read_card_photo_bytes(u["id"], cid, raw)
    if not got:
        raise HTTPException(404, "no photo")
    data, kind = got
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
        "Clappers PC photo report",
        f"Profile photo report\nBinder: {slug}\nReporter: {who} (id {uid})\nReason: {reason}\nUnique reports: {n}\n(3 unique reports hide the photo.)",
    )
    if hidden:
        send_mail(
            "Clappers PC photo removed",
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
    row = con.execute("SELECT email,display,hue,bio,avatar,cropx,cropy,cropz,socials,book_hist FROM users WHERE id=?", (uid,)).fetchone()
    con.close()
    hist = []
    try:
        hist = json.loads(row["book_hist"] if row else "[]") or []
    except Exception:
        hist = []
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
        "book_hist": hist if isinstance(hist, list) else [],
    }

@app.post("/book-hist")
async def save_book_hist(payload: dict, request: Request, x_token: str | None = Header(default=None)):
    uid = require_user(request, x_token)
    pts = payload.get("points") or []
    clean = []
    seen = set()
    for p in pts:
        if not isinstance(p, dict):
            continue
        d = str(p.get("d") or "")[:10]
        try:
            v = float(p.get("v"))
        except (TypeError, ValueError):
            continue
        if len(d) != 10 or v < 0:
            continue
        seen.add(d)
        clean.append({"d": d, "v": round(v, 2), "t": str(p.get("t") or "")[:28]})
    clean = clean[-400:]
    con = db()
    try:
        con.execute("UPDATE users SET book_hist=? WHERE id=?", (json.dumps(clean), uid))
        con.commit()
    except Exception:
        pass
    con.close()
    return {"ok": True, "n": len(clean)}


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
    taken = None
    if display.lower() != "collector":
        taken = con.execute(
            "SELECT id FROM users WHERE id!=? AND lower(trim(IFNULL(display,'')))=lower(trim(?))",
            (uid, display),
        ).fetchone()
    if taken:
        con.close()
        raise HTTPException(409, "that screen name is taken")
    con.execute(
        "UPDATE users SET display=?, hue=?, bio=?, avatar=?, cropx=?, cropy=?, cropz=?, socials=? WHERE id=?",
        (display, hue, bio, avatar, cropx, cropy, cropz, json.dumps(socials), uid),
    )
    con.commit()
    con.close()
    return {"ok": True, "display": display, "hue": hue, "bio": bio, "avatar": avatar, "socials": socials}

@app.get("/ledgerz")
async def ledgerz(q: str = "", request: Request = None):
    qn = re.sub(r"\s+", " ", (q or "").strip().lower())
    if len(qn) < 2:
        return {"cards": []}
    con = db()
    users = {r["id"]: r for r in con.execute("SELECT id,slug,display,IFNULL(suspended,0) AS suspended FROM users").fetchall()}
    rows = con.execute("SELECT user_id, data FROM cards ORDER BY id DESC LIMIT 4000").fetchall()
    con.close()
    out = []
    for r in rows:
        u = users.get(r["user_id"])
        if not u or int(u["suspended"] or 0):
            continue
        try:
            c = json.loads(r["data"])
        except Exception:
            continue
        hay = " ".join(str(c.get(k) or "") for k in ("player","team","set","year","number","insert","parallel")).lower()
        if qn not in hay and not all(p in hay for p in qn.split()):
            continue
        out.append({
            "id": c.get("id"),
            "player": c.get("player"),
            "team": c.get("team"),
            "year": c.get("year"),
            "set": c.get("set"),
            "number": c.get("number"),
            "grader": c.get("grader"),
            "grade": c.get("grade"),
            "comp": c.get("sysComp") or c.get("comp"),
            "slug": u["slug"],
            "display": u["display"] or "Collector",
        })
        if len(out) >= 80:
            break
    return {"cards": out}

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
        if photo_file_exists(u["id"], c.get("id") or ""):
            c["has_photo"] = True
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
    slug = ensure_slug(uid)
    who = display_of(con, uid)
    if slug:
        for f in con.execute("SELECT follower FROM follows WHERE slug=?", (slug,)).fetchall():
            fid = f["follower"]
            if fid and fid != uid:
                add_note(con, fid, slug, who + " posted a banner: " + title)
    con.commit()
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
        "Clappers PC user report",
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
        "Clappers PC comment report",
        f"Comment report\nComment id: {cid}\nBinder: {row['slug']}\nReporter: {who} (id {uid})\nReason: {reason}\nText: {(row['body'] or '')[:200]}\nUnique reports: {n}\n(3 unique reports hide the comment.)",
    )
    if hidden:
        send_mail(
            "Clappers PC comment removed",
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
    send_mail("Clappers PC photo restored", f"Profile photo restored for binder: {slug}")
    return {"ok": True}

@app.post("/admin/restore-comment")
async def admin_restore_comment(payload: dict, request: Request, x_token: str | None = Header(default=None)):
    require_operator(request, x_token)
    cid = int(payload.get("id") or 0)
    con = db()
    con.execute("UPDATE comments SET hidden=0 WHERE id=?", (cid,))
    con.commit()
    con.close()
    send_mail("Clappers PC comment restored", f"Comment {cid} restored")
    return {"ok": True}

@app.post("/admin/hide-comment")
async def admin_hide(payload: dict, request: Request, x_token: str | None = Header(default=None)):
    require_operator(request, x_token)
    cid = int(payload.get("id") or 0)
    con = db()
    con.execute("UPDATE comments SET hidden=1 WHERE id=?", (cid,))
    con.commit()
    con.close()
    send_mail("Clappers PC comment hidden", f"Comment {cid} hidden by operator")
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
        row = con.execute("SELECT * FROM users WHERE lower(email)=?", (raw,)).fetchone()
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
    send_mail("Clappers PC banner removed", f"Banner cleared for {slug}")
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
    send_mail("Clappers PC photo hidden", f"Profile photo hidden for {slug}")
    return {"ok": True}

def _find_user(con, payload: dict):
    email = (payload.get("email") or "").strip().lower()
    slug = re.sub(r"[^a-z0-9]", "", (payload.get("slug") or "").lower())
    row = None
    if email:
        row = con.execute("SELECT id,email,slug FROM users WHERE lower(email)=?", (email,)).fetchone()
    if not row and slug:
        row = con.execute("SELECT id,email,slug FROM users WHERE slug=?", (slug,)).fetchone()
    return row

@app.post("/admin/mail")
async def admin_mail(payload: dict, request: Request, x_token: str | None = Header(default=None)):
    require_operator(request, x_token)
    subject = str(payload.get("subject") or "").strip()[:200]
    body = str(payload.get("body") or "").strip()[:8000]
    if not subject or not body:
        raise HTTPException(400, "subject and message required")
    to = str(payload.get("to") or "").strip().lower()
    if "@" not in to:
        con = db()
        row = _find_user(con, {"email": to, "slug": to} if to else payload)
        con.close()
        if not row:
            raise HTTPException(404, "no user")
        to = row["email"]
    ok = send_mail(subject, body, to)
    if not ok:
        raise HTTPException(502, MAIL_LAST_ERROR or "mail failed")
    return {"ok": True, "to": to}

@app.post("/admin/announce")
async def admin_announce(payload: dict, request: Request, x_token: str | None = Header(default=None)):
    require_operator(request, x_token)
    raw = str(payload.get("body") or "").strip()
    if not raw:
        raise HTTPException(400, "message required")
    text = "Clappers Management: " + raw
    text = text[:500]
    who = str(payload.get("to") or "").strip()
    con = db()
    if who:
        row = _find_user(con, {"email": who, "slug": who})
        if not row:
            con.close()
            raise HTTPException(404, "no user")
        add_note(con, row["id"], row["slug"] if "slug" in row.keys() else "", text)
        n = 1
    else:
        ids = con.execute("SELECT id, slug FROM users").fetchall()
        for u in ids:
            add_note(con, u["id"], u["slug"] if "slug" in u.keys() else "", text)
        n = len(ids)
    con.commit()
    con.close()
    return {"ok": True, "sent": n}

@app.post("/admin/mail-all")
async def admin_mail_all(payload: dict, request: Request, x_token: str | None = Header(default=None)):
    require_operator(request, x_token)
    subject = str(payload.get("subject") or "").strip()[:200]
    body = str(payload.get("body") or "").strip()[:8000]
    if not subject or not body:
        raise HTTPException(400, "subject and message required")
    con = db()
    rows = con.execute("SELECT email FROM users WHERE email IS NOT NULL AND email != ''").fetchall()
    con.close()
    sent = 0
    fail = 0
    for r in rows:
        em = (r["email"] or "").strip().lower()
        if "@" not in em:
            continue
        if send_mail(subject, body, em):
            sent += 1
        else:
            fail += 1
    return {"ok": True, "sent": sent, "fail": fail}

@app.post("/admin/plan")
async def admin_plan(payload: dict, request: Request, x_token: str | None = Header(default=None)):
    require_operator(request, x_token)
    want = "plus" if str(payload.get("plan") or "").lower() in ("plus", "on", "1", "true") else "free"
    con = db()
    row = _find_user(con, payload)
    if not row:
        con.close()
        raise HTTPException(404, "no user with that email or slug")
    if want == "plus":
        until = now_utc() + timedelta(days=30)
        mark = until.strftime("%Y-%m-%dT%H:%M:%SZ")
        start = now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")
        con.execute(
            "UPDATE users SET plan='plus', plus_until=?, cycle_start=? WHERE id=?",
            (mark, start, row["id"]),
        )
    else:
        con.execute("UPDATE users SET plan='free', plus_until=NULL WHERE id=?", (row["id"],))
    con.commit()
    con.close()
    send_mail("Clappers PC plan "+want, f"{row['email']} / {row['slug'] if 'slug' in row.keys() else ''}")
    return {"ok": True, "email": row["email"], "slug": row["slug"], "plan": want}

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
    send_mail("Clappers PC account "+("suspended" if on else "restored"), f"{row['email']} / {row['slug']}")
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
    send_mail("Clappers PC account deleted", f"{row['email']} / {slug} removed")
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
    send_mail("Clappers PC account deleted", f"{row['email']} / {slug} deleted their account")
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
