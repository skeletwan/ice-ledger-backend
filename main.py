import os, json, base64, time
from io import BytesIO
from fastapi import FastAPI, UploadFile, File, Header, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse
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
If it is a graded slab, read the label. If raw, identify player, year, set, card number, parallel.
If a field is not readable, use null. Do not invent a rare parallel.
{
  "player": string|null,
  "year": string|null,
  "set": string|null,
  "number": string|null,
  "parallel": string|null,
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

@app.post("/identify")
async def identify(
    file: UploadFile = File(...),
    x_app_secret: str | None = Header(default=None),
    secret: str | None = Form(default=None),
):
    check_secret(x_app_secret or secret)
    if not XAI_API_KEY:
        raise HTTPException(500, "XAI_API_KEY not set on server")
    check_cap()
    raw = await file.read()
    if len(raw) > 12_000_000:
        raise HTTPException(400, "image too large")
    try:
        jpeg = shrink(raw)
    except Exception:
        raise HTTPException(400, "not a readable image")
    b64 = base64.b64encode(jpeg).decode("ascii")
    payload = {
        "model": MODEL,
        "temperature": 0,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "high"}},
                    {"type": "text", "text": PROMPT},
                ],
            }
        ],
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
