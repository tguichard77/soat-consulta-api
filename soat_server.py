#!/usr/bin/env python3
"""
Servidor FastAPI para consulta de SOAT via APESEG.
Expone: POST /consulta  { "placa": "9840LD" }
Devuelve: { "estado": ..., "aseguradora": ..., "vigencia_fin": ... }
Usa 2captcha para resolver captchas (sin EasyOCR, liviano en memoria).
Compatible con pydantic v1.
"""

import requests, warnings, base64, json, os, time
from typing import Optional
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

warnings.filterwarnings("ignore")

BASE_URL        = "https://webapp.apeseg.org.pe"
CAPTCHA_SECRET  = "9asjKZ9aJq1@2025"
LOGIN_EMAIL     = "notificaciones@apeseg.org.pe"
LOGIN_PASSWORD  = "G3sepa13579!"
TOKEN_CACHE     = "/tmp/apeseg_token.json"
TWOCAPTCHA_KEY  = os.environ.get("TWOCAPTCHA_KEY", "")

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
    estado: Optional[str]
    aseguradora: Optional[str]
    vigencia_fin: Optional[str]


# ── captcha via 2captcha ──────────────────────────────────────────────────────

def resolver_captcha_2captcha(img_base64: str):
    """Envía imagen a 2captcha y espera el resultado."""
    if not TWOCAPTCHA_KEY:
        return None
    try:
        if "," in img_base64:
            img_base64 = img_base64.split(",", 1)[1]

        r = requests.post("https://2captcha.com/in.php", data={
            "key": TWOCAPTCHA_KEY,
            "method": "base64",
            "body": img_base64,
            "json": 1,
        }, timeout=30)
        data = r.json()
        if data.get("status") != 1:
            print("❌ 2captcha error al enviar: {}".format(data), flush=True)
            return None

        captcha_id = data["request"]
        print("📤 Captcha enviado a 2captcha, id={}".format(captcha_id), flush=True)

        for _ in range(20):
            time.sleep(3)
            r2 = requests.get("https://2captcha.com/res.php", params={
                "key": TWOCAPTCHA_KEY,
                "action": "get",
                "id": captcha_id,
                "json": 1,
            }, timeout=15)
            d2 = r2.json()
            if d2.get("status") == 1:
                texto = d2["request"].strip().lower()
                print("✅ 2captcha resolvió: '{}'".format(texto), flush=True)
                return texto
            if d2.get("request") != "CAPCHA_NOT_READY":
                print("❌ 2captcha error: {}".format(d2), flush=True)
                return None

        print("❌ 2captcha timeout", flush=True)
        return None
    except Exception as e:
        print("❌ 2captcha excepción: {}".format(e), flush=True)
        return None


# ── auth helpers ──────────────────────────────────────────────────────────────

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
        "{}/consulta-soat/api/login".format(BASE_URL),
        headers={
            "Content-Type": "application/json",
            "Referer": "{}/consulta-soat/?source=soat".format(BASE_URL),
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
    """Resuelve captcha con 2captcha, hace login y devuelve token."""
    for intento in range(1, 6):
        print("🔄 Intento captcha {}/5...".format(intento), flush=True)
        r = session.get(
            "{}/captcha-api/api/captcha".format(BASE_URL),
            headers={
                "X-App-Secret": CAPTCHA_SECRET,
                "Referer": "{}/consulta-soat/?source=soat".format(BASE_URL),
                "Origin": BASE_URL,
                "User-Agent": "Mozilla/5.0 Chrome/120",
            },
            timeout=15
        )
        d = r.json()
        key = d["key"]
        img_data = d["img"]

        texto = resolver_captcha_2captcha(img_data)
        if not texto:
            continue

        r2 = session.post(
            "{}/captcha-api/api/captcha/verify".format(BASE_URL),
            headers={
                "Content-Type": "application/json",
                "X-App-Secret": CAPTCHA_SECRET,
                "Referer": "{}/consulta-soat/?source=soat".format(BASE_URL),
                "Origin": BASE_URL,
            },
            json={"captcha": texto, "key": key},
            timeout=15
        )
        if r2.json().get("valid", False):
            print("✅ Captcha verificado: '{}'".format(texto), flush=True)
            token, err = do_login(session)
            if token:
                save_token(token)
                return token
            raise RuntimeError("Login fallido: {}".format(err))
        else:
            print("❌ Captcha incorrecto: '{}'".format(texto), flush=True)

    raise RuntimeError("No se pudo resolver el captcha después de 5 intentos")


def parse_fecha(fecha_str):
    if not fecha_str:
        return None
    try:
        parts = fecha_str.split("/")
        if len(parts) == 3:
            return "{}-{}-{}".format(parts[2], parts[1].zfill(2), parts[0].zfill(2))
    except:
        pass
    return fecha_str


# ── endpoints ─────────────────────────────────────────────────────────────────

@app.post("/consulta", response_model=ConsultaResponse)
def consultar(req: ConsultaRequest):
    placa = req.placa.strip().upper()
    print("🔍 Consultando placa: {}".format(placa), flush=True)

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
    if len(placa_clean) >= 6:
        formatos.append(placa_clean[:4] + "-" + placa_clean[4:])

    headers_consulta = {
        "Authorization": "Bearer {}".format(token),
        "X-Source": "soat",
        "X-Referrer": "https://www.soat.com.pe/",
        "Referer": "{}/consulta-soat/poliza".format(BASE_URL),
        "Origin": BASE_URL,
        "User-Agent": "Mozilla/5.0 Chrome/120",
    }

    for fmt in formatos:
        r = session.get(
            "{}/consulta-soat/api/certificados/placa/{}".format(BASE_URL, fmt),
            headers=headers_consulta,
            timeout=20
        )

        if r.status_code in (401, 403):
            print("🔄 Token expirado, renovando...", flush=True)
            clear_token()
            try:
                token = resolver_captcha_y_login(session)
                headers_consulta["Authorization"] = "Bearer {}".format(token)
            except RuntimeError as e:
                raise HTTPException(status_code=500, detail=str(e))
            r = session.get(
                "{}/consulta-soat/api/certificados/placa/{}".format(BASE_URL, fmt),
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
    return {"status": "ok", "2captcha": bool(TWOCAPTCHA_KEY)}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
