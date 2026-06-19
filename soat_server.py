#!/usr/bin/env python3
"""
Servidor FastAPI para consulta de SOAT via APESEG.
Expone: POST /consulta  { "placa": "9840LD" }
Devuelve: { "estado": ..., "aseguradora": ..., "vigencia_fin": ... }
"""

import sys, requests, warnings, base64, json, os, time
from PIL import Image, ImageEnhance
import io, numpy as np
import easyocr
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

warnings.filterwarnings("ignore")

BASE_URL       = "https://webapp.apeseg.org.pe"
CAPTCHA_SECRET = "9asjKZ9aJq1@2025"
LOGIN_EMAIL    = "notificaciones@apeseg.org.pe"
LOGIN_PASSWORD = "G3sepa13579!"
TOKEN_CACHE    = "/tmp/apeseg_token.json"

# Inicializar EasyOCR una sola vez al arrancar
print("🔄 Cargando EasyOCR...", flush=True)
READER = easyocr.Reader(['en'], gpu=False, verbose=False)
print("✅ EasyOCR listo.", flush=True)

app = FastAPI(title="SOAT Consulta API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

class ConsultaRequest(BaseModel):
    placa: str

class ConsultaResponse(BaseModel):
    estado: str | None
    aseguradora: str | None
    vigencia_fin: str | None


# ── helpers ──────────────────────────────────────────────────────────────────

def leer_captcha(raw_bytes):
    resultados = []
    img = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
    for scale in [4, 6]:
        v = img.resize((img.width * scale, img.height * scale), Image.LANCZOS)
        for contrast in [1.0, 2.0, 3.0]:
            vi = ImageEnhance.Contrast(v).enhance(contrast)
            arr = np.array(vi)
            try:
                texts = READER.readtext(
                    arr, detail=0, paragraph=False,
                    allowlist='abcdefghijklmnopqrstuvwxyz0123456789',
                    min_size=5
                )
                texto = ''.join(texts).replace(' ', '').lower().strip()
                if 4 <= len(texto) <= 8 and texto not in resultados:
                    resultados.append(texto)
            except:
                pass
    return resultados


def get_cached_token():
    if os.path.exists(TOKEN_CACHE):
        try:
            with open(TOKEN_CACHE) as f:
                data = json.load(f)
            if time.time() - data.get("ts", 0) < 3000:
                return data.get("token")
        except:
            pass
    return None


def save_token(token):
    try:
        with open(TOKEN_CACHE, "w") as f:
            json.dump({"token": token, "ts": time.time()}, f)
    except:
        pass


def clear_token():
    try:
        os.remove(TOKEN_CACHE)
    except:
        pass


def do_login(session):
    r = session.post(
        f"{BASE_URL}/consulta-soat/api/login",
        headers={
            "Content-Type": "application/json",
            "Referer": f"{BASE_URL}/consulta-soat/?source=soat",
            "Origin": BASE_URL,
            "User-Agent": "Mozilla/5.0 Chrome/120",
        },
        json={"email": LOGIN_EMAIL, "password": LOGIN_PASSWORD},
        timeout=20
    )
    data = r.json()
    token = data.get("access_token")
    if not token:
        return None, data.get("message", str(data))
    return token, None


def resolver_captcha_y_login(session):
    """Resuelve captcha, hace login y devuelve token. Lanza excepción si falla."""
    captcha_ok = None
    captcha_key = None

    for intento in range(1, 40):
        r = session.get(
            f"{BASE_URL}/captcha-api/api/captcha",
            headers={
                "X-App-Secret": CAPTCHA_SECRET,
                "Referer": f"{BASE_URL}/consulta-soat/?source=soat",
                "Origin": BASE_URL,
                "User-Agent": "Mozilla/5.0 Chrome/120",
            },
            timeout=15
        )
        d = r.json()
        key = d["key"]
        img_data = d["img"]
        raw = base64.b64decode(img_data.split(",", 1)[1] if "," in img_data else img_data)

        candidatos = leer_captcha(raw)
        for texto in candidatos:
            r2 = session.post(
                f"{BASE_URL}/captcha-api/api/captcha/verify",
                headers={
                    "Content-Type": "application/json",
                    "X-App-Secret": CAPTCHA_SECRET,
                    "Referer": f"{BASE_URL}/consulta-soat/?source=soat",
                    "Origin": BASE_URL,
                },
                json={"captcha": texto, "key": key},
                timeout=15
            )
            if r2.json().get("valid", False):
                captcha_ok = texto
                captcha_key = key
                break
        if captcha_ok:
            print(f"✅ Captcha resuelto en intento {intento}", flush=True)
            break

    if not captcha_ok:
        raise RuntimeError("No se pudo resolver el captcha después de 39 intentos")

    token, err = do_login(session)
    if not token:
        raise RuntimeError(f"Login fallido: {err}")

    save_token(token)
    return token


def parse_fecha(fecha_str):
    if not fecha_str:
        return None
    try:
        parts = fecha_str.split("/")
        if len(parts) == 3:
            return f"{parts[2]}-{parts[1].zfill(2)}-{parts[0].zfill(2)}"
    except:
        pass
    return fecha_str


# ── endpoint ─────────────────────────────────────────────────────────────────

@app.post("/consulta", response_model=ConsultaResponse)
def consultar(req: ConsultaRequest):
    placa = req.placa.strip().upper()
    print(f"🔍 Consultando placa: {placa}", flush=True)

    session = requests.Session()
    session.verify = False

    token = get_cached_token()
    if token:
        print("♻️  Token cacheado OK", flush=True)
    else:
        print("⏳ Login fresco necesario...", flush=True)
        try:
            token = resolver_captcha_y_login(session)
        except RuntimeError as e:
            raise HTTPException(status_code=500, detail=str(e))

    placa_clean = ''.join(c for c in placa if c.isalnum())
    formatos = [placa_clean]
    if len(placa_clean) == 6:
        formatos.append(placa_clean[:4] + "-" + placa_clean[4:])
    elif len(placa_clean) == 7:
        formatos.append(placa_clean[:4] + "-" + placa_clean[4:])

    headers_consulta = {
        "Authorization": f"Bearer {token}",
        "X-Source": "soat",
        "X-Referrer": "https://www.soat.com.pe/",
        "Referer": f"{BASE_URL}/consulta-soat/poliza",
        "Origin": BASE_URL,
        "User-Agent": "Mozilla/5.0 Chrome/120",
    }

    for fmt in formatos:
        r = session.get(
            f"{BASE_URL}/consulta-soat/api/certificados/placa/{fmt}",
            headers=headers_consulta,
            timeout=20
        )

        if r.status_code in (401, 403):
            print("🔄 Token expirado, renovando...", flush=True)
            clear_token()
            try:
                token = resolver_captcha_y_login(session)
                headers_consulta["Authorization"] = f"Bearer {token}"
            except RuntimeError as e:
                raise HTTPException(status_code=500, detail=str(e))
            r = session.get(
                f"{BASE_URL}/consulta-soat/api/certificados/placa/{fmt}",
                headers=headers_consulta,
                timeout=20
            )

        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list) and len(data) > 0:
                cert = data[0]
                return ConsultaResponse(
                    estado=(cert.get("Estado") or "").upper() or None,
                    aseguradora=cert.get("NombreCompania") or None,
                    vigencia_fin=parse_fecha(cert.get("FechaFin"))
                )

    return ConsultaResponse(estado=None, aseguradora=None, vigencia_fin=None)


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
