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
from pydantic import BaseModel

# 1. MODELY (Musia byť presné)
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

# 2. POMOCNÉ FUNKCIE
def parse_llm_response(raw: str) -> dict:
    raw = raw.strip()
    if "```json" in raw:
        raw = raw.split("```json")[1].split("```")[0]
    elif "```" in raw:
        raw = raw.split("```")[1].split("```")[0]
    return json.loads(raw)

def blocek_to_xlsx(blocek: Bloček) -> io.BytesIO:
    rows = [{"Dodávateľ": blocek.dodavatel, "Dátum": blocek.datum, "Položka": p.nazov, "Suma s DPH": p.suma_s_dph} for p in blocek.polozky]
    df = pd.DataFrame(rows)
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Bloček")
    buffer.seek(0)
    return buffer

# 3. APP A CORS
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

@app.post("/upload", response_model=Bloček)
async def upload_receipt(file: UploadFile = File(...)):
    try:
        image_data = await file.read()
        b64_data = base64.b64encode(image_data).decode("utf-8")
        
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        
        # Používame stabilnú verziu modelu
        response = client.messages.create(
            model="claude-3-5-sonnet-20240620",
            max_tokens=2048,
            system="Vráť VÝHRADNE JSON podľa schémy. Žiadne reči okolo.",
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64_data}},
                {"type": "text", "text": "Extrahuj: dodavatel, datum, language_code, polozky (nazov, suma_s_dph, suma_bez_dph, dph_vyska, dph_sadzba_percento), total_suma."}
            ]}]
        )
        
        parsed_data = parse_llm_response(response.content[0].text)
        return Bloček(**parsed_data)
    except Exception as e:
        print(f"ERROR: {str(e)}") # Toto uvidíš v Logoch na Renderi
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/export-xlsx")
async def export_xlsx(blocek: Bloček):
    return StreamingResponse(
        blocek_to_xlsx(blocek),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="blocek.xlsx"'}
    )
