import base64
import io
import json
import os
from typing import List, Optional

import anthropic
import pandas as pd
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

# ── 1. PYDANTIC MODELY (Striktne podľa tvojho pôvodného frontendu) ──────────

class Polozka(BaseModel):
    nazov: str
    suma_s_dph: float
    suma_bez_dph: float
    dph_vyska: float
    dph_sadzba_percento: float

class Bloček(BaseModel):
    dodavatel: str
    datum: str = Field(..., description="ISO 8601 formát, napr. 2024-03-15")
    language_code: str = Field(..., min_length=2, max_length=5)
    polozky: List[Polozka]
    total_suma: float

# ── 2. SYSTÉMOVÝ PROMPT (Tvoj pôvodný "mozog" aplikácie) ────────────────────

SYSTEM_PROMPT = """Si expert na spracovanie bločkov a faktúr z celého sveta.
Extrahuj dáta z obrázku bločku a vráť VÝHRADNE čistý JSON bez akéhokoľvek textu okolo.

PRAVIDLÁ:
1. JAZYK: Detekuj jazyk (sk, de, de-AT, en...). Názvy položiek NEPREKLADAJ.
2. DÁTUM: Vždy konvertuj na YYYY-MM-DD.
3. SUMY: Vždy float s desatinnou bodkou. Ignoruj symboly mien.

Schéma JSON:
{
  "dodavatel": "názov firmy",
  "datum": "YYYY-MM-DD",
  "language_code": "sk",
  "polozky": [
    {
      "nazov": "názov položky",
      "suma_s_dph": 0.0,
      "suma_bez_dph": 0.0,
      "dph_vyska": 0.0,
      "dph_sadzba_percento": 20.0
    }
  ],
  "total_suma": 0.0
}"""

# ── 3. POMOCNÉ FUNKCIE (Logika spracovania) ──────────────────────────────────

def parse_llm_response(raw: str) -> dict:
    """Ošetrí odpoveď od AI, ak by tam boli markdown značky."""
    raw = raw.strip()
    if "```json" in raw:
        raw = raw.split("```json")[1].split("```")[0]
    elif "```" in raw:
        raw = raw.split("```")[1].split("```")[0]
    return json.loads(raw)

def blocek_to_xlsx(blocek: Bloček) -> io.BytesIO:
    """Vytvorí Excel súbor z prijatých dát."""
    rows = []
    for p in blocek.polozky:
        rows.append({
            "Dodávateľ": blocek.dodavatel,
            "Dátum": blocek.datum,
            "Jazyk": blocek.language_code,
            "Položka": p.nazov,
            "Suma s DPH (€)": p.suma_s_dph,
            "Suma bez DPH (€)": p.suma_bez_dph,
            "DPH (%)": p.dph_sadzba_percento
        })
    
    df = pd.DataFrame(rows)
    # Pridáme súhrnný riadok na koniec
    summary = pd.DataFrame([{"Dodávateľ": "SPOLU", "Suma s DPH (€)": blocek.total_suma}])
    
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Položky")
        summary.to_excel(writer, index=False, sheet_name="Súhrn")
    buffer.seek(0)
    return buffer

# ── 4. FASTAPI A CORS ───────────────────────────────────────────────────────

app = FastAPI(title="Bloček Scanner Pro")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── 5. ENDPOINTY (Cesty) ───────────────────────────────────────────────────

@app.post("/upload", response_model=Bloček)
async def upload_receipt(file: UploadFile = File(...)):
    """Hlavný bod pre analýzu fotky."""
    try:
        content = await file.read()
        b64_image = base64.b64encode(content).decode("utf-8")
        
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        
        message = client.messages.create(
            model="claude-3-5-sonnet-20240620",
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64_image}},
                    {"type": "text", "text": "Analyzuj tento bloček a vráť JSON."}
                ]
            }]
        )
        
        data = parse_llm_response(message.content[0].text)
        return Bloček(**data)
        
    except Exception as e:
        print(f"DEBUG CHYBA: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Chyba servera: {str(e)}")

@app.post("/export-xlsx")
async def export_xlsx(blocek: Bloček):
    """Bleskový export do Excelu z JSON dát."""
    try:
        xlsx_file = blocek_to_xlsx(blocek)
        filename = f"blocek_{blocek.dodavatel.replace(' ', '_')}.xlsx"
        
        return StreamingResponse(
            xlsx_file,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'}
        )
    except Exception as e:
        raise HTTPException(status_code=500,
