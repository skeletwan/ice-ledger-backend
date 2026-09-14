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


COMP_MODEL = os.environ.get("COMP_MODEL", "grok-4-1-fast-reasoning")
COMP_PROMPT = """Find recent SOLD prices (not asking prices) for this exact hockey card.
Use eBay sold/completed and 130point if possible.
Card: {card}
Return ONLY JSON:
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
suggested_cad is your best single number in Canadian dollars if you can convert; otherwise null.
Do not use listing ask prices. If solds don't match the parallel or grade, needs_review true and suggested_cad null.
"""

def _extract_response_text(body: dict) -> str:
    if "output_text" in body and body["output_text"]:
        return body["output_text"]
    chunks = []
    for item in body.get("output") or []:
        if item.get("type") == "message":
            for c in item.get("content") or []:
                if c.get("type") in ("output_text", "text") and c.get("text"):
                    chunks.append(c["text"])
    if chunks:
        return "\n".join(chunks)
    try:
        return body["choices"][0]["message"]["content"]
    except Exception:
        return ""

@app.post("/comp")
async def comp(
    payload: dict,
    x_app_secret: str | None = Header(default=None),
):
    check_secret(x_app_secret or payload.get("secret"))
    if not XAI_API_KEY:
        raise HTTPException(500, "XAI_API_KEY not set on server")
    check_cap()
    card = {k: payload.get(k) for k in ("player","year","set","number","parallel","team","grader","grade","cert")}
    label = ", ".join(f"{k}={v}" for k,v in card.items() if v)
    req = {
        "model": COMP_MODEL,
        "tools": [{"type": "web_search", "allowed_domains": ["ebay.ca","ebay.com","130point.com","psacard.com"]}],
        "input": COMP_PROMPT.format(card=label),
    }
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(
            "https://api.x.ai/v1/responses",
            headers={"Authorization": f"Bearer {XAI_API_KEY}", "Content-Type": "application/json"},
            json=req,
        )
    if r.status_code >= 400:
        raise HTTPException(502, f"xAI comp error {r.status_code}: {r.text[:400]}")
    text = _extract_response_text(r.json()).strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text[: text.rfind("```")]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise HTTPException(502, "comp did not return JSON")
    data["model"] = COMP_MODEL
    data["card"] = card
    return data

