#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SIMULADOR del endpoint que se le pediria a infraestructura de Omarsa.

Que hace
--------
POST /upload-from-url
    Recibe la URL de un archivo en temp-contents de Qlik mas una API key de
    Qlik, lo descarga, lo sube a api-upload/upload/file-temp con
    multipart/form-data y devuelve el mismo {"data": {"name": "..."}} de ese
    endpoint, mas "mediaUrl" ya armado.
    CONFIRMADO (2026-09-21) que funciona: un archivo subido por la conexion
    del conector a temp-contents SI se puede leer despues con una API key
    normal del mismo usuario, mientras la key este vigente.

GET /health  -> {"ok": true}

Por que existe
--------------
Qlik Automate no puede armar una peticion multipart/form-data, asi que no
puede subir la imagen directamente al endpoint de Omarsa. Lo que si puede
hacer es subir la imagen a su propio almacenamiento temporal (temp-contents)
y despues un POST con JSON (bloque Call URL). Este servicio recibe esa URL,
descarga el archivo y lo vuelve a subir a un storage que Omarsa/WhatsApp si
pueden leer sin credenciales.

Contrato completo: ver README.md.

Nota
----
Existe una segunda ruta (/render-and-upload), que en vez de recibir una URL
genera la imagen ella misma con la Reports API de Qlik a partir de una
descripcion (app, objeto, filtros). Quedo mas flexible pero sin usar, asi que
no esta en este archivo: esta guardada como respaldo local en
respaldo-local/app_render_and_upload.py.

Variables de entorno
--------------------
  SIM_TOKEN   obligatoria. Cualquier cadena larga inventada.
  PORT        la pone Render sola.
"""

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPLOAD_URL = "https://a1-api-upl-lb.omarsa.com.ec/api-upload/upload/file-temp"
VIEW_BASE = "https://a1-api-view-lb.omarsa.com.ec/api-view/view/media-temp"
PROVIDER = "OMARSA"
CHANNEL = "informe-bi"

# Dominios de los que este servicio esta dispuesto a descargar un archivo.
# Sin esto seria un descargador de URLs arbitrarias con credenciales detras.
DOMINIOS_DESCARGA = (".qlikcloud.com", ".omarsa.com.ec")

MAX_BYTES = 25 * 1024 * 1024  # 25 MB, de sobra para un PNG de tablero


def log(*a):
    """Escribe una linea en los logs del servicio (los que se ven en Render)."""
    print(*a, flush=True)


class ErrorPeticion(Exception):
    """Error atribuible a quien llama al servicio (falta un campo, dominio no
    permitido, etc). Se traduce en HTTP 400 o 403, segun 'codigo'."""
    def __init__(self, mensaje, codigo=400):
        super().__init__(mensaje)
        self.codigo = codigo


class ErrorEtapa(Exception):
    """Error ocurrido a mitad del proceso (fallo la descarga o la subida).
    Se traduce siempre en HTTP 502, con la etapa y el detalle incluidos en
    la respuesta para poder diagnosticar sin mirar los logs."""
    def __init__(self, etapa, mensaje, detalle=None):
        super().__init__(mensaje)
        self.etapa = etapa
        self.detalle = detalle


def http(url, method="GET", data=None, headers=None, timeout=120):
    """Hace una peticion HTTP simple con la libreria estandar.
    Devuelve (status, headers, body) SIEMPRE, incluso en 4xx/5xx: nunca
    lanza excepcion por un codigo de error, solo por fallos de red."""
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def a_json(body):
    """Intenta interpretar 'body' (bytes) como JSON. Si no se puede, None
    en vez de lanzar excepcion, para no romper el manejo de errores."""
    try:
        return json.loads(body.decode("utf-8")) if body else None
    except Exception:
        return None


def recortar(body, n=800):
    """Prepara un cuerpo de respuesta para meterlo dentro de un error: si es
    JSON lo deja tal cual, si es texto/binario lo recorta a 'n' caracteres
    para que el detalle del error no quede enorme."""
    if isinstance(body, (bytes, bytearray)):
        parsed = a_json(body)
        return parsed if parsed is not None else body.decode("utf-8", "replace")[:n]
    return body


def multipart(campos, archivos):
    """Arma a mano un cuerpo multipart/form-data (campos de texto + archivos
    binarios), porque la libreria estandar de Python no trae un armador
    listo y esto evita instalar una dependencia externa."""
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


def dominio_descarga_permitido(url):
    """True si el host de 'url' esta en la lista blanca de dominios de los
    que este servicio acepta descargar (*.qlikcloud.com, *.omarsa.com.ec)."""
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return any(host == d.lstrip(".") or host.endswith(d) for d in DOMINIOS_DESCARGA)


def subir_a_omarsa(contenido, nombre, mime, gw_key):
    """Sube un archivo ya descargado al almacenamiento temporal de Omarsa
    (file-temp) y arma el mediaUrl publico a partir del nombre que devuelve.
    Es el paso final, comun a cualquier origen del archivo."""
    cuerpo, content_type = multipart(
        {"json_data": json.dumps({"path": ""})},
        {"file": (nombre, contenido, mime)},
    )
    st, _, body = http(UPLOAD_URL, "POST", cuerpo, {
        "x-provider": PROVIDER, "x-channel": CHANNEL, "x-api-key": gw_key,
        "Content-Type": content_type}, timeout=180)
    parsed = a_json(body)
    if st not in (200, 201) or not parsed or not (parsed.get("data") or {}).get("name"):
        log("fallo la subida: HTTP %s %s" % (st, body[:300]))
        raise ErrorEtapa("subida", "la subida a Omarsa fallo (HTTP %s)" % st, recortar(body))
    name = parsed["data"]["name"]
    log("subida OK: %s" % name)
    salida = dict(parsed)
    salida["mediaUrl"] = "%s/%s" % (VIEW_BASE, name)
    return salida


class Handler(BaseHTTPRequestHandler):
    """Servidor HTTP minimo (sin frameworks) con las rutas GET /health y
    POST /upload-from-url."""

    server_version = "SimuladorOmarsa/2.1"

    def _responder(self, codigo, payload):
        """Envia 'payload' como JSON con el codigo HTTP indicado."""
        cuerpo = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def log_message(self, formato, *args):
        """Redirige el log de acceso propio de BaseHTTPRequestHandler a
        nuestra funcion log(), para que todo salga junto en Render."""
        log("%s - %s" % (self.address_string(), formato % args))

    def do_GET(self):
        """Unica ruta GET: /health, para saber si el servicio esta despierto
        (util porque el plan gratuito de Render lo duerme por inactividad)."""
        if self.path.rstrip("/") in ("", "/health"):
            self._responder(200, {"ok": True, "servicio": "simulador omarsa",
                                  "rutas": ["POST /upload-from-url"]})
        else:
            self._responder(404, {"error": True, "message": "ruta no encontrada"})

    def _autorizar(self):
        """Valida el x-sim-token (evita que esto sea un proxy abierto) y
        extrae la x-api-key del gateway de la peticion. Si algo falla, ya
        responde el error y devuelve None; si todo esta bien, devuelve la
        key del gateway para que el llamador siga con la subida."""
        esperado = os.environ.get("SIM_TOKEN", "").strip()
        if not esperado:
            self._responder(500, {"error": True, "message": "el servicio no tiene SIM_TOKEN"})
            return None
        if self.headers.get("x-sim-token", "") != esperado:
            log("rechazado: x-sim-token invalido")
            self._responder(401, {"error": True, "message": "x-sim-token invalido o ausente"})
            return None
        gw_key = self.headers.get("x-api-key", "").strip()
        if not gw_key:
            self._responder(400, {"error": True,
                                  "message": "falta la cabecera x-api-key (key del gateway)"})
            return None
        return gw_key

    def _leer_json(self):
        """Lee el cuerpo de la peticion y lo interpreta como JSON. Limita el
        tamano a 1 MB (de sobra para este cuerpo, que es solo texto)."""
        largo = int(self.headers.get("Content-Length", "0") or 0)
        if largo > 1024 * 1024:
            raise ErrorPeticion("cuerpo demasiado grande")
        try:
            return json.loads(self.rfile.read(largo).decode("utf-8")) if largo else {}
        except Exception as e:
            raise ErrorPeticion("JSON invalido: %s" % e)

    def do_POST(self):
        """Unica ruta POST: /upload-from-url. Valida la ruta y la
        autorizacion, y traduce cualquier error de las funciones internas
        (ErrorPeticion, ErrorEtapa, o lo que sea) a una respuesta JSON con
        el codigo HTTP correcto."""
        ruta = self.path.split("?")[0].rstrip("/")
        if ruta != "/upload-from-url":
            return self._responder(404, {"error": True, "message": "ruta no encontrada"})
        gw_key = self._autorizar()
        if not gw_key:
            return
        try:
            datos = self._leer_json()
            return self._upload_from_url(datos, gw_key)
        except ErrorPeticion as e:
            log("peticion invalida: %s" % e)
            return self._responder(e.codigo, {"error": True, "etapa": "validacion",
                                              "message": str(e)})
        except ErrorEtapa as e:
            log("fallo en %s: %s" % (e.etapa, e))
            return self._responder(502, {"error": True, "etapa": e.etapa,
                                         "message": str(e), "detalle": e.detalle})
        except Exception as e:
            log("error inesperado: %s: %s" % (type(e).__name__, e))
            return self._responder(500, {"error": True, "etapa": "interno",
                                         "message": "%s: %s" % (type(e).__name__, e)})

    def _upload_from_url(self, datos, gw_key):
        """Logica de /upload-from-url: valida la url y el dominio, descarga
        el archivo (con la cabecera Authorization que mande el llamador) y
        lo sube a Omarsa. Es el unico metodo que hace el trabajo real de
        esta ruta; do_POST solo se encarga de la parte HTTP."""
        url = (datos.get("url") or "").strip()
        filename = re.sub(r"[^A-Za-z0-9._-]", "_",
                          (datos.get("filename") or "").strip() or "imagen.png")[:120]
        if not url.lower().startswith("https://"):
            raise ErrorPeticion("'url' falta o no es https")
        if not dominio_descarga_permitido(url):
            raise ErrorPeticion("dominio no permitido: %s" % (DOMINIOS_DESCARGA,), 403)
        log("descargando %s" % url)
        headers = {}
        if datos.get("authorization"):
            headers["Authorization"] = datos["authorization"].strip()
        st, rh, contenido = http(url, "GET", None, headers)
        if st != 200:
            raise ErrorEtapa("descarga", "no se pudo descargar el archivo (HTTP %s)" % st,
                             recortar(contenido))
        if len(contenido) > MAX_BYTES:
            raise ErrorEtapa("descarga", "el archivo supera %d MB" % (MAX_BYTES // 1024 // 1024))
        mime = rh.get("Content-Type", "application/octet-stream")
        if mime.startswith("application/octet-stream") and filename.lower().endswith(".png"):
            mime = "image/png"
        return self._responder(200, subir_a_omarsa(contenido, filename, mime, gw_key))


def main():
    """Punto de entrada: arranca el servidor HTTP en el puerto que indique
    Render (o 10000 si se corre localmente)."""
    puerto = int(os.environ.get("PORT", "10000"))
    if not os.environ.get("SIM_TOKEN", "").strip():
        log("AVISO: SIM_TOKEN no esta definido. El servicio respondera 500 a todo.")
    log("escuchando en el puerto %d" % puerto)
    ThreadingHTTPServer(("0.0.0.0", puerto), Handler).serve_forever()


if __name__ == "__main__":
    main()
