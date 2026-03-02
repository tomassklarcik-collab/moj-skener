import base64, io, json, os, anthropic, pandas as pd
from typing import List
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

# 1. Pôvodné modely (tak ako si ich mal predtým)
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

app = FastAPI()

# Pôvodné CORS nastavenie
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Pomocná funkcia pre Excel
def generate_xlsx(blocek: Bloček) -> io.BytesIO:
    rows = []
    for p in blocek.polozky:
        rows.append({
            "Dodávateľ": blocek.dodavatel,
            "Dátum": blocek.datum,
            "Položka": p.nazov,
            "Suma s DPH (€)": p.suma_s_dph,
            "Sadzba DPH (%)": p.dph_sadzba_percento,
        })
    df = pd.DataFrame(rows)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Bloček")
    output.seek(0)
    return output

@app.post("/upload", response_model=Bloček)
async def upload_receipt(file: UploadFile = File(...)):
    image_data = await file.read()
    b64_data = base64.b64encode(image_data).decode("utf-8")
    
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    
    # Používame tvoj pôvodný systémový prompt (skrátený pre stabilitu)
    response = client.messages.create(
        model="claude-3-5-sonnet-20240620",
        max_tokens=2048,
        system="Si expert na bločky. Vráť VÝHRADNE JSON podľa schémy Bloček.",
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64_data}},
            {"type": "text", "text": "Extrahuj dáta z bločku do JSONu."}
        ]}]
    )
    
    res_text = response.content[0].text.strip()
    if "```json" in res_text: res_text = res_text.split("```json")[1].split("```")[0]
    return Bloček(**json.loads(res_text))

# TÁTO FUNKCIA JE KĽÚČOVÁ PRE EXCEL
@app.post("/export-xlsx")
async def export_xlsx(blocek: Bloček):
    xlsx_buffer = generate_xlsx(blocek)
    return StreamingResponse(
        xlsx_buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="vypis_blocek.xlsx"'}
    )
