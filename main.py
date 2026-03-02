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


# ── Pydantic modely ──────────────────────────────────────────────────────────

class Polozka(BaseModel):
    nazov: str
    suma_s_dph: float
    suma_bez_dph: float
    dph_vyska: float
    dph_sadzba_percento: float


class Bloček(BaseModel):
    dodavatel: str
    datum: str = Field(..., description="ISO 8601 formát, napr. 2024-03-15")
    language_code: str = Field(
        ...,
        description="ISO 639-1 kód jazyka bločku, napr. 'sk', 'de', 'at', 'en'",
        min_length=2,
        max_length=5,
    )
    polozky: List[Polozka]
    total_suma: float


# ── Pomocné funkcie ──────────────────────────────────────────────────────────

SYSTEM_PROMPT = """Si expert na spracovanie bločkov a faktúr z celého sveta.
Extrahuj dáta z obrázku bločku a vráť VÝHRADNE čistý JSON bez akéhokoľvek textu okolo.
Žiadne markdown bloky, žiadne komentáre – iba surový JSON objekt.

PRAVIDLÁ SPRACOVANIA:

1. JAZYK A NÁZVY POLOŽIEK
   - Detekuj jazyk bločku a zaznamenaj ho ako ISO 639-1 kód (napr. "sk", "de", "cs", "en", "fr", "it", "pl").
   - Pre rakúske bločky (nemecký jazyk + AT adresa / EUR mena) použi "de-AT".
   - Názvy položiek (`nazov`) zachovaj PRESNE tak, ako sú napísané na bločku – vrátane veľkých písmen,
     skratiek, diakritiky a prípadných preklepov. NEPREKLADAJ a NEOPRAVUJ.

2. NORMALIZÁCIA DÁTUMOV
   - Dátum vždy konvertuj do formátu YYYY-MM-DD bez ohľadu na lokálny formát na bločku.
   - Príklady vstupov → výstup:
       "15.03.2024"  → "2024-03-15"   (DE/SK/AT formát)
       "03/15/2024"  → "2024-03-15"   (US formát)
       "15-Mar-2024" → "2024-03-15"   (anglický skratkový formát)
       "2024年3月15日" → "2024-03-15"  (japonský formát)
   - Ak dátum chýba, použi dnešný dátum vo formáte YYYY-MM-DD.

3. NORMALIZÁCIA SÚM
   - Všetky peňažné hodnoty vráť ako číslo s desatinnou bodkou (float), nie čiarkou.
   - Príklady: "1.234,56 €" → 1234.56 | "1,234.56" → 1234.56 | "1 234,56" → 1234.56
   - Ignoruj symboly mien (€, $, CHF…) – vráť iba číselnú hodnotu.
   - Ak suma chýba, použi 0.0.

Schéma JSON (striktne dodržuj kľúče):
{
  "dodavatel": "<názov firmy presne z bločku>",
  "datum": "<YYYY-MM-DD>",
  "language_code": "<ISO 639-1, napr. sk | de | de-AT | en | cs>",
  "polozky": [
    {
      "nazov": "<názov položky presne z bločku>",
      "suma_s_dph": <float>,
      "suma_bez_dph": <float>,
      "dph_vyska": <float>,
      "dph_sadzba_percento": <float>
    }
  ],
  "total_suma": <float>
}"""


def image_to_base64(data: bytes, content_type: str) -> tuple[str, str]:
    """Zakóduje bytes obrázku do base64 a vráti (media_type, base64_data)."""
    allowed = {"image/jpeg", "image/png", "image/gif", "image/webp"}
    media_type = content_type if content_type in allowed else "image/jpeg"
    return media_type, base64.standard_b64encode(data).decode("utf-8")


def parse_llm_response(raw: str) -> Bloček:
    """Parsuje raw JSON string z LLM a validuje cez Pydantic."""
    raw = raw.strip()
    # Odstrán prípadné markdown code fences
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=422, detail=f"LLM vrátil neplatný JSON: {e}")
    try:
        return Bloček(**data)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Validácia zlyhala: {e}")


def blocek_to_xlsx(blocek: Bloček) -> io.BytesIO:
    """Transformuje Bloček model na XLSX buffer pomocou pandas."""
    rows = []
    for p in blocek.polozky:
        rows.append({
            "Dodávateľ": blocek.dodavatel,
            "Dátum": blocek.datum,
            "Jazyk bločku": blocek.language_code,
            "Názov položky": p.nazov,
            "Suma s DPH (€)": p.suma_s_dph,
            "Suma bez DPH (€)": p.suma_bez_dph,
            "Výška DPH (€)": p.dph_vyska,
            "Sadzba DPH (%)": p.dph_sadzba_percento,
        })

    df_items = pd.DataFrame(rows)

    # Súhrnný riadok
    summary = pd.DataFrame([{
        "Dodávateľ": blocek.dodavatel,
        "Dátum": blocek.datum,
        "Jazyk bločku": blocek.language_code,
        "Názov položky": "CELKOVÁ SUMA",
        "Suma s DPH (€)": blocek.total_suma,
        "Suma bez DPH (€)": "",
        "Výška DPH (€)": "",
        "Sadzba DPH (%)": "",
    }])

    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df_items.to_excel(writer, sheet_name="Položky", index=False)
        summary.to_excel(writer, sheet_name="Súhrn", index=False)

        # Autofit stĺpcov
        for sheet_name in writer.sheets:
            ws = writer.sheets[sheet_name]
            for col in ws.columns:
                max_len = max((len(str(cell.value or "")) for cell in col), default=10)
                ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 50)

    buffer.seek(0)
    return buffer


# ── FastAPI app ──────────────────────────────────────────────────────────────

app = FastAPI(
    title="Bloček Scanner API",
    description="API pre mobilný skener bločkov pomocou Claude Vision",
    version="1.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Toto povolí prístup z akejkoľvek adresy (vrátane tvojho GitHubu)
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post(
    "/upload-receipt",
    response_model=Bloček,
    summary="Nahraj obrázok bločku a získaj štruktúrované dáta",
)
async def upload_receipt(file: UploadFile = File(...)):
    """
    Príjme obrázok bločku, pošle ho do Claude Vision API
    a vráti extrahované dáta v JSON formáte.
    """
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Súbor musí byť obrázok (image/*).")

    image_data = await file.read()
    if len(image_data) > 20 * 1024 * 1024:  # 20 MB limit
        raise HTTPException(status_code=413, detail="Obrázok je príliš veľký (max 20 MB).")

    media_type, b64_data = image_to_base64(image_data, file.content_type)

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    try:
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": b64_data,
                            },
                        },
                        {
                            "type": "text",
                            "text": "Extrahuj všetky dáta z tohto bločku a vráť ich ako čistý JSON.",
                        },
                    ],
                }
            ],
        )
    except anthropic.APIError as e:
        raise HTTPException(status_code=502, detail=f"Chyba Anthropic API: {e}")

    raw_text = message.content[0].text
    blocek = parse_llm_response(raw_text)
    return blocek


@app.post(
    "/upload-receipt/export-xlsx",
    summary="Nahraj obrázok a stiahni XLSX export",
)
async def upload_receipt_xlsx(file: UploadFile = File(...)):
    """
    Rovnaké spracovanie ako /upload-receipt, ale vráti
    priamo XLSX súbor na stiahnutie.
    """
    blocek: Bloček = await upload_receipt(file)
    xlsx_buffer = blocek_to_xlsx(blocek)

    filename = f"blocek_{blocek.dodavatel.replace(' ', '_')}_{blocek.datum}.xlsx"
    return StreamingResponse(
        xlsx_buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/health")
async def health():
    return {"status": "ok"}
