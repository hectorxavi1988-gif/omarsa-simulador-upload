#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SIMULADOR del endpoint que se le pediria a infraestructura de Omarsa.

Que hace
--------
Recibe un JSON con la URL de un archivo y las credenciales para descargarlo,
lo descarga, lo sube a api-upload/upload/file-temp con multipart/form-data, y
devuelve el mismo {"data": {"name": "..."}} que devuelve ese endpoint.

Existe porque Qlik Automate NO puede armar una peticion multipart/form-data, asi
que no puede subir la imagen del tablero directamente. Pero SI puede:
  - subir la imagen a su propio almacenamiento temporal (probado: 311 KB)
  - hacer POST con cuerpo JSON a un endpoint externo (probado)
Entonces se invierte la direccion: Qlik le pasa una URL a este servicio, y el
servicio hace la descarga y la subida.

Contrato
--------
POST /upload-from-url
  Headers:
    Content-Type: application/json
    x-sim-token: <SIM_TOKEN>        # para que no sea un proxy abierto
    x-api-key:   <key del gateway>  # key dinamica de generate-app
  Body:
    {
      "url": "https://omarsa.us.qlikcloud.com/api/v1/temp-contents/<id>",
      "authorization": "Bearer <api key de Qlik>",
      "filename": "tablero.png"      # opcional
    }
  Respuesta 200:
    {"statusCode": 200, "message": "OK",
     "data": {"name": "<uuid>.png", "mimetype": "image/png", "file_code": null},
     "mediaUrl": "https://a3-api-view.omarsa.com.ec/api-view/view/media-temp/<uuid>.png"}

Se devuelve tambien "mediaUrl" ya armado, por comodidad: asi la automation de
Qlik no tiene que concatenar nada.

GET /health -> {"ok": true}

Notas de diseno
---------------
- Este simulador NO guarda ninguna credencial de Omarsa. La key del gateway se
  la pasa quien llama (Qlik la genera antes con generate-app, que ya probamos
  que funciona desde el bloque Call URL). El endpoint definitivo de
  infraestructura probablemente la manejaria internamente, porque vive dentro
  de su red; para una prueba es mejor que no queden secretos aqui.
- El x-sim-token evita que cualquiera con la URL lo use para subir archivos a
  Omarsa. Se define como variable de entorno en Render.
- Solo se permite descargar de dominios de la lista blanca, para que esto no
  sea un descargador de URLs arbitrarias.

Variables de entorno
--------------------
  SIM_TOKEN   obligatoria. Cualquier cadena larga inventada.
  PORT        la pone Render sola.
"""

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPLOAD_URL = "https://a3-api-upl.omarsa.com.ec/api-upload/upload/file-temp"
VIEW_BASE = "https://a3-api-view.omarsa.com.ec/api-view/view/media-temp"
PROVIDER = "OMARSA"
CHANNEL = "informe-bi"

# Solo se descargan archivos de estos dominios.
DOMINIOS_PERMITIDOS = (".qlikcloud.com", ".omarsa.com.ec")

MAX_BYTES = 25 * 1024 * 1024  # 25 MB, de sobra para un PNG de tablero


def log(*a):
    print(*a, flush=True)


def dominio_permitido(url):
    try:
        host = urllib.parse.urlparse(url).hostname or ""
    except Exception:
        return False
    host = host.split(":")[0].lower()
    return any(host == d.lstrip(".") or host.endswith(d) for d in DOMINIOS_PERMITIDOS)


def descargar(url, authorization):
    req = urllib.request.Request(url, method="GET")
    if authorization:
        req.add_header("Authorization", authorization)
    with urllib.request.urlopen(req, timeout=120) as r:
        datos = r.read(MAX_BYTES + 1)
        if len(datos) > MAX_BYTES:
            raise ValueError("el archivo supera el limite de %d MB" % (MAX_BYTES // 1024 // 1024))
        return datos, r.headers.get("Content-Type", "application/octet-stream")


def multipart(campos, archivos):
    b = uuid.uuid4().hex
    out = bytearray()
    for k, v in campos.items():
        out += b"--" + b.encode() + b"\r\n"
        out += ('Content-Disposition: form-data; name="%s"\r\n\r\n' % k).encode("utf-8")
        out += v.encode("utf-8") + b"\r\n"
    for k, (nombre, contenido, mime) in archivos.items():
        out += b"--" + b.encode() + b"\r\n"
        out += ('Content-Disposition: form-data; name="%s"; filename="%s"\r\n'
                % (k, nombre)).encode("utf-8")
        out += ("Content-Type: %s\r\n\r\n" % mime).encode("utf-8")
        out += contenido + b"\r\n"
    out += b"--" + b.encode() + b"--\r\n"
    return bytes(out), "multipart/form-data; boundary=" + b


def subir_a_omarsa(contenido, nombre, mime, gw_key):
    cuerpo, content_type = multipart(
        {"json_data": json.dumps({"path": ""})},
        {"file": (nombre, contenido, mime)},
    )
    req = urllib.request.Request(UPLOAD_URL, data=cuerpo, method="POST")
    req.add_header("x-provider", PROVIDER)
    req.add_header("x-channel", CHANNEL)
    req.add_header("x-api-key", gw_key)
    req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class Handler(BaseHTTPRequestHandler):
    server_version = "SimuladorOmarsa/1.0"

    def _responder(self, codigo, payload):
        cuerpo = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def log_message(self, formato, *args):
        log("%s - %s" % (self.address_string(), formato % args))

    def do_GET(self):
        if self.path.rstrip("/") in ("", "/health"):
            self._responder(200, {"ok": True, "servicio": "simulador upload-from-url"})
        else:
            self._responder(404, {"error": True, "message": "ruta no encontrada"})

    def do_POST(self):
        if self.path.rstrip("/") != "/upload-from-url":
            return self._responder(404, {"error": True, "message": "ruta no encontrada"})

        token_esperado = os.environ.get("SIM_TOKEN", "").strip()
        if not token_esperado:
            return self._responder(500, {"error": True,
                                         "message": "el servicio no tiene SIM_TOKEN configurado"})
        if self.headers.get("x-sim-token", "") != token_esperado:
            log("rechazado: x-sim-token invalido")
            return self._responder(401, {"error": True, "message": "x-sim-token invalido o ausente"})

        gw_key = self.headers.get("x-api-key", "").strip()
        if not gw_key:
            return self._responder(400, {"error": True,
                                         "message": "falta la cabecera x-api-key (key del gateway)"})

        try:
            largo = int(self.headers.get("Content-Length", "0"))
            datos = json.loads(self.rfile.read(largo).decode("utf-8")) if largo else {}
        except Exception as e:
            return self._responder(400, {"error": True, "message": "JSON invalido: %s" % e})

        url = (datos.get("url") or "").strip()
        authorization = (datos.get("authorization") or "").strip()
        filename = (datos.get("filename") or "").strip() or "imagen.png"
        filename = re.sub(r'[^A-Za-z0-9._-]', "_", filename)[:120]

        if not url:
            return self._responder(400, {"error": True, "message": "falta 'url' en el cuerpo"})
        if not url.lower().startswith("https://"):
            return self._responder(400, {"error": True, "message": "la url debe ser https"})
        if not dominio_permitido(url):
            log("rechazado: dominio no permitido -> %s" % url)
            return self._responder(403, {
                "error": True,
                "message": "dominio no permitido. Solo se aceptan %s" % (DOMINIOS_PERMITIDOS,)})

        log("descargando %s" % url)
        try:
            contenido, mime = descargar(url, authorization)
        except urllib.error.HTTPError as e:
            log("fallo la descarga: HTTP %s" % e.code)
            return self._responder(502, {
                "error": True,
                "message": "no se pudo descargar el archivo (HTTP %s). Revisar la cabecera "
                           "'authorization' del cuerpo." % e.code})
        except Exception as e:
            log("fallo la descarga: %s" % e)
            return self._responder(502, {"error": True,
                                         "message": "no se pudo descargar el archivo: %s" % e})

        if mime.startswith("application/octet-stream") and filename.lower().endswith(".png"):
            mime = "image/png"
        log("descargado %d bytes (%s), subiendo a Omarsa" % (len(contenido), mime))

        status, respuesta = subir_a_omarsa(contenido, filename, mime, gw_key)
        try:
            parsed = json.loads(respuesta.decode("utf-8"))
        except Exception:
            parsed = None

        if status not in (200, 201) or not parsed or not parsed.get("data", {}).get("name"):
            log("fallo la subida: HTTP %s %s" % (status, respuesta[:300]))
            return self._responder(502, {
                "error": True,
                "message": "la subida a Omarsa fallo (HTTP %s)" % status,
                "respuestaOmarsa": parsed if parsed else respuesta.decode("utf-8", "replace")[:500]})

        name = parsed["data"]["name"]
        log("subida OK: %s" % name)
        salida = dict(parsed)
        salida["mediaUrl"] = "%s/%s" % (VIEW_BASE, name)
        return self._responder(200, salida)


def main():
    puerto = int(os.environ.get("PORT", "10000"))
    if not os.environ.get("SIM_TOKEN", "").strip():
        log("AVISO: SIM_TOKEN no esta definido. El servicio respondera 500 a todo.")
    log("escuchando en el puerto %d" % puerto)
    ThreadingHTTPServer(("0.0.0.0", puerto), Handler).serve_forever()


if __name__ == "__main__":
    main()
