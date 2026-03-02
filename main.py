import base64
import io
import json
import os
from typing import List

import anthropic
import pandas as pd
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

# ── Modely ──────────────────────────────────────────────────────────────────

class Polozka(BaseModel):
    nazov: str
    suma_s_dph: float
    suma_bez_dph: float
    dph_vyska: float
    dph_sadzba_percento: float

class Bloček(BaseModel):
    dodavatel: str
    datum: str
    language_code: str
    polozky: List[Polozka]
    total_suma: float

# ── Premenné a Prompt ────────────────────────────────────────────────────────

SYSTEM_PROMPT = """Si expert na spracovanie bločkov. Extrahuj dáta a vráť VÝHRADNE čistý JSON.
Pravidlá: Dátum vráť v ISO formáte YYYY-MM-DD. Sumy ako float s bodkou. 
Názvy položiek ponechaj v pôvodnom znení."""

def blocek_to_xlsx(blocek: Bloček) -> io.BytesIO:
    rows = []
    for p in blocek.polozky:
        rows.append({
            "Dodávateľ": blocek.dodavatel,
            "Dátum": blocek.datum,
            "Položka": p.nazov,
            "Suma s DPH": p.suma_s_dph,
            "DPH %": p.dph_sadzba_percento
        })
    
    df = pd.DataFrame(rows)
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Bloček")
    buffer.seek(0)
    return buffer

# ── API Aplikácia ────────────────────────────────────────────────────────────

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.post("/upload", response_model=Bloček)
async def upload_receipt(file: UploadFile = File(...)):
    image_data = await file.read()
    b64_data = base64.b64encode(image_data).decode("utf-8")
    
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    
    response = client.messages.create(
        model="claude-3-5-sonnet-20240620",
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64_data}},
            {"type": "text", "text": "Analyzuj bloček."}
        ]}]
    )
    
    # Vyčistenie JSONu z odpovede
    text = response.content[0].text.strip()
    if "```json" in text: text = text.split("```json")[1].split("```")[0]
    return json.loads(text)

@app.post("/export-xlsx")
async def export_xlsx(blocek: Bloček):
    xlsx_buffer = blocek_to_xlsx(blocek)
    filename = f"blocek_{blocek.dodavatel.replace(' ', '_')}.xlsx"
    return StreamingResponse(
        xlsx_buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )

@app.get("/health")
async def health(): return {"status": "ok"}
