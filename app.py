#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SIMULADOR del endpoint que se le pediria a infraestructura de Omarsa.

Que hace
--------
POST /upload-from-url  (la que usa hoy la automatizacion de Qlik)
    Recibe la URL de un archivo en temp-contents de Qlik mas una API key de
    Qlik, lo descarga, lo sube a api-upload/upload/file-temp con
    multipart/form-data y devuelve el mismo {"data": {"name": "..."}} de ese
    endpoint, mas "mediaUrl" ya armado.
    CONFIRMADO (2026-09-21) que funciona: un archivo subido por la conexion
    del conector a temp-contents SI se puede leer despues con una API key
    normal del mismo usuario, mientras la key este vigente. El 404 que se
    vio antes era por una key vencida, no por una restriccion de identidad.

POST /render-and-upload  (alternativa, para cuando se necesite flexibilidad)
    Recibe en JSON la DESCRIPCION de una imagen de Qlik Cloud (tenant, app,
    objeto, tamano, filtros) en vez de una URL, la genera ella misma con la
    Reports API de Qlik, y sube el resultado igual que la ruta de arriba.
    Sirve para cualquier app y cualquier objeto/hoja PUBLICADA de cualquier
    tenant *.qlikcloud.com, sin que la automatizacion tenga que subir nada a
    temp-contents primero. Hoy no la usa la automatizacion en produccion,
    pero queda lista por si se necesita pedir imagenes de otras apps.

GET /health  -> {"ok": true}

Por que existe
--------------
Qlik Automate no puede armar una peticion multipart/form-data, asi que no puede
subir la imagen al endpoint de Omarsa. Si puede hacer POST con JSON (bloque Call
URL). Entonces Qlik solo describe la imagen que quiere; el servicio la genera,
la sube y devuelve el nombre.

Contrato completo: ver README.md.

Variables de entorno
--------------------
  SIM_TOKEN   obligatoria. Cualquier cadena larga inventada.
  PORT        la pone Render sola.
"""

import datetime as dt
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPLOAD_URL = "https://a3-api-upl.omarsa.com.ec/api-upload/upload/file-temp"
VIEW_BASE = "https://a3-api-view.omarsa.com.ec/api-view/view/media-temp"
PROVIDER = "OMARSA"
CHANNEL = "informe-bi"

DOMINIOS_DESCARGA = (".qlikcloud.com", ".omarsa.com.ec")   # solo /upload-from-url
DOMINIO_QLIK = ".qlikcloud.com"                             # tenants aceptados

MAX_BYTES = 25 * 1024 * 1024
ESPERA_RENDER_S = 150          # tope de espera del render en Qlik
ANCHO_MAX = ALTO_MAX = 6000    # proteccion contra lienzos absurdos

TZ_ECUADOR = dt.timezone(dt.timedelta(hours=-5))
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio",
         "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]

ESTRATEGIAS = ("failOnErrors", "ignoreErrorsReturnDetails", "ignoreErrorsNoDetails")
TIPOS_OBJETO = ("visualization", "sheet")


def log(*a):
    print(*a, flush=True)


class ErrorPeticion(Exception):
    """Error atribuible a quien llama -> 400/403."""
    def __init__(self, mensaje, codigo=400):
        super().__init__(mensaje)
        self.codigo = codigo


class ErrorEtapa(Exception):
    """Error en una etapa del proceso -> 502, con la etapa y el detalle."""
    def __init__(self, etapa, mensaje, detalle=None):
        super().__init__(mensaje)
        self.etapa = etapa
        self.detalle = detalle


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def http(url, method="GET", data=None, headers=None, timeout=120):
    """Devuelve (status, headers, body). No lanza excepcion en 4xx/5xx."""
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def a_json(body):
    try:
        return json.loads(body.decode("utf-8")) if body else None
    except Exception:
        return None


def recortar(body, n=800):
    if isinstance(body, (bytes, bytearray)):
        parsed = a_json(body)
        return parsed if parsed is not None else body.decode("utf-8", "replace")[:n]
    return body


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


# --------------------------------------------------------------------------
# Marcadores de periodo: {{anio}}, {{mes:+1}}, {{hoy:-1}}, ...
# --------------------------------------------------------------------------
_MARCADOR = re.compile(r"\{\{\s*(anio|mes|Mes|MES|mesnum|mesnum2|hoy)\s*(?::\s*([+-]?\d+))?\s*\}\}")


def resolver_marcadores(texto, ahora=None):
    """Reemplaza marcadores de periodo, calculados en hora de Ecuador.

    {{anio}} {{anio:+1}}    año del mes desplazado N meses       -> 2026
    {{mes}}  {{mes:+1}}     nombre del mes, minusculas           -> octubre
    {{Mes}}  / {{MES}}      igual, Capitalizado / MAYUSCULAS     -> Octubre / OCTUBRE
    {{mesnum}}              numero de mes sin cero               -> 10
    {{mesnum2}}             numero de mes con dos digitos        -> 10 / 09
    {{hoy}} {{hoy:-1}}      fecha desplazada N DIAS, AAAA-MM-DD  -> 2026-09-20
    """
    if not isinstance(texto, str) or "{{" not in texto:
        return texto
    hoy = (ahora or dt.datetime.now(TZ_ECUADOR)).date()

    def rep(m):
        clave, off = m.group(1), int(m.group(2) or 0)
        if clave == "hoy":
            return (hoy + dt.timedelta(days=off)).isoformat()
        total = hoy.year * 12 + (hoy.month - 1) + off
        anio, idx = total // 12, total % 12
        if clave == "anio":
            return str(anio)
        if clave == "mesnum":
            return str(idx + 1)
        if clave == "mesnum2":
            return "%02d" % (idx + 1)
        nombre = MESES[idx]
        return nombre.capitalize() if clave == "Mes" else nombre.upper() if clave == "MES" else nombre

    return _MARCADOR.sub(rep, texto)


def resolver_profundo(valor):
    if isinstance(valor, str):
        return resolver_marcadores(valor)
    if isinstance(valor, list):
        return [resolver_profundo(v) for v in valor]
    if isinstance(valor, dict):
        return {k: resolver_profundo(v) for k, v in valor.items()}
    return valor


# --------------------------------------------------------------------------
# Validacion de la solicitud de imagen
# --------------------------------------------------------------------------
def validar_solicitud(datos):
    if not isinstance(datos, dict):
        raise ErrorPeticion("el cuerpo debe ser un objeto JSON")

    qlik = datos.get("qlik") or {}
    img = datos.get("imagen") or {}
    arch = datos.get("archivo") or {}
    if not isinstance(qlik, dict) or not isinstance(img, dict) or not isinstance(arch, dict):
        raise ErrorPeticion("'qlik', 'imagen' y 'archivo' deben ser objetos")

    tenant = (qlik.get("tenant") or "").strip().lower()
    tenant = re.sub(r"^https?://", "", tenant).rstrip("/")
    if not tenant:
        raise ErrorPeticion("falta qlik.tenant (ej. omarsa.us.qlikcloud.com)")
    if not re.fullmatch(r"[a-z0-9.-]+", tenant) or not tenant.endswith(DOMINIO_QLIK):
        raise ErrorPeticion("qlik.tenant debe ser un host *%s" % DOMINIO_QLIK, 403)

    auth = (qlik.get("authorization") or "").strip()
    if not auth:
        raise ErrorPeticion("falta qlik.authorization ('Bearer <api key de Qlik>')")
    if not auth.lower().startswith("bearer "):
        auth = "Bearer " + auth

    app_id = (img.get("appId") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9-]{8,64}", app_id):
        raise ErrorPeticion("imagen.appId falta o no es valido")

    objeto = (img.get("objetoId") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", objeto):
        raise ErrorPeticion("imagen.objetoId falta o no es valido")

    tipo = (img.get("tipo") or "visualization").strip()
    if tipo not in TIPOS_OBJETO:
        raise ErrorPeticion("imagen.tipo debe ser uno de %s" % (TIPOS_OBJETO,))

    def entero(nombre, defecto, minimo, maximo):
        v = img.get(nombre, defecto)
        try:
            v = int(v)
        except (TypeError, ValueError):
            raise ErrorPeticion("imagen.%s debe ser entero" % nombre)
        if not minimo <= v <= maximo:
            raise ErrorPeticion("imagen.%s fuera de rango (%d-%d)" % (nombre, minimo, maximo))
        return v

    ancho = entero("ancho", 1280, 100, ANCHO_MAX)
    alto = entero("alto", 720, 100, ALTO_MAX)
    dpi = entero("dpi", 96, 72, 300)
    try:
        zoom = float(img.get("zoom", 1))
    except (TypeError, ValueError):
        raise ErrorPeticion("imagen.zoom debe ser numero")
    if not 0.25 <= zoom <= 4:
        raise ErrorPeticion("imagen.zoom fuera de rango (0.25-4)")

    estrategia = img.get("estrategiaSeleccion") or "failOnErrors"
    if estrategia not in ESTRATEGIAS:
        raise ErrorPeticion("imagen.estrategiaSeleccion debe ser uno de %s" % (ESTRATEGIAS,))

    # Selecciones: forma simple, o selectionsByState crudo (avanzado)
    selections_by_state = None
    aplicadas = []
    if img.get("selectionsByState") is not None:
        if not isinstance(img["selectionsByState"], dict):
            raise ErrorPeticion("imagen.selectionsByState debe ser un objeto")
        selections_by_state = resolver_profundo(img["selectionsByState"])
        aplicadas = "selectionsByState (crudo)"
    else:
        sels = img.get("selecciones") or []
        if not isinstance(sels, list):
            raise ErrorPeticion("imagen.selecciones debe ser una lista")
        por_estado = {}
        for i, s in enumerate(sels):
            if not isinstance(s, dict) or not s.get("campo"):
                raise ErrorPeticion("imagen.selecciones[%d] necesita 'campo'" % i)
            valores = s.get("valores")
            if not isinstance(valores, list) or not valores:
                raise ErrorPeticion("imagen.selecciones[%d].valores debe ser una lista no vacia" % i)
            numerico = bool(s.get("numerico", False))
            estado = s.get("estado") or "$"
            vals = []
            for v in valores:
                v = resolver_marcadores(v) if isinstance(v, str) else v
                if numerico:
                    try:
                        num = float(v)
                    except (TypeError, ValueError):
                        raise ErrorPeticion("imagen.selecciones[%d]: '%s' no es numerico" % (i, v))
                    vals.append({"number": int(num) if num.is_integer() else num,
                                 "isNumeric": True})
                else:
                    vals.append({"text": str(v), "isNumeric": False})
            por_estado.setdefault(estado, []).append({
                "fieldName": s["campo"], "defaultIsNumeric": numerico, "values": vals})
            aplicadas.append({"campo": s["campo"], "estado": estado,
                              "valores": [x.get("text", x.get("number")) for x in vals]})
        selections_by_state = por_estado

    nombre = resolver_marcadores((arch.get("nombre") or "").strip()) or \
        "qlik-%s-%s.png" % (objeto, dt.datetime.now(TZ_ECUADOR).strftime("%Y%m%d-%H%M"))
    nombre = re.sub(r"[^A-Za-z0-9._-]", "_", nombre)[:120]
    if not nombre.lower().endswith(".png"):
        nombre += ".png"

    return {
        "tenant": tenant, "auth": auth, "appId": app_id, "objeto": objeto, "tipo": tipo,
        "ancho": ancho, "alto": alto, "dpi": dpi, "zoom": zoom,
        "estrategia": estrategia, "selectionsByState": selections_by_state,
        "aplicadas": aplicadas, "nombre": nombre,
    }


# --------------------------------------------------------------------------
# Render con la Reports API de Qlik
# --------------------------------------------------------------------------
def renderizar(s):
    base = "https://" + s["tenant"]
    headers = {"Authorization": s["auth"], "Content-Type": "application/json"}
    template = {
        "appId": s["appId"],
        "selectionStrategy": s["estrategia"],
        "visualization": {"id": s["objeto"], "type": s["tipo"],
                          "widthPx": s["ancho"], "heightPx": s["alto"]},
    }
    if s["selectionsByState"]:
        template["selectionType"] = "selectionsByState"
        template["selectionsByState"] = s["selectionsByState"]
    cuerpo = {
        "type": "sense-image-1.0",
        "senseImageTemplate": template,
        "output": {"outputId": "img", "type": "image",
                   "imageOutput": {"outFormat": "png", "outDpi": s["dpi"], "outZoom": s["zoom"]}},
    }

    log("render: tenant=%s app=%s objeto=%s tipo=%s %dx%d sel=%s" % (
        s["tenant"], s["appId"], s["objeto"], s["tipo"], s["ancho"], s["alto"],
        json.dumps(s["aplicadas"], ensure_ascii=False)))
    st, rh, body = http(base + "/api/v1/reports", "POST",
                        json.dumps(cuerpo).encode("utf-8"), headers)
    if st == 401:
        raise ErrorEtapa("render", "Qlik rechazo la API key (HTTP 401)", recortar(body))
    if st not in (200, 201, 202):
        raise ErrorEtapa("render", "Qlik rechazo la solicitud de imagen (HTTP %s)" % st,
                         recortar(body))
    location = rh.get("Location") or rh.get("location")
    if not location:
        raise ErrorEtapa("render", "Qlik no devolvio la cabecera Location", recortar(body))
    if location.startswith("/"):
        location = base + location

    inicio = time.time()
    estado = {}
    while time.time() - inicio < ESPERA_RENDER_S:
        time.sleep(2)
        st, _, body = http(location, "GET", None, {"Authorization": s["auth"]})
        estado = a_json(body) or {}
        if st != 200:
            raise ErrorEtapa("render", "error consultando el estado (HTTP %s)" % st, recortar(body))
        if estado.get("status") == "done":
            break
        if estado.get("status") == "failed":
            texto = json.dumps(estado)
            pista = None
            if "empty GenericType" in texto or "chart not found" in texto:
                pista = ("Qlik no encontro el objeto. La Reports API solo ve hojas PUBLICADAS: "
                         "publicar la hoja que contiene el objeto y revisar objetoId/tipo.")
            elif "selection" in texto.lower():
                pista = ("Revisar las selecciones: nombre exacto del campo, y 'numerico' segun "
                         "como este cargado el campo en la app.")
            raise ErrorEtapa("render", "Qlik reporto 'failed' al generar la imagen",
                             {"pista": pista, "qlik": estado})
    else:
        raise ErrorEtapa("render", "se agoto la espera (%d s) del render" % ESPERA_RENDER_S, estado)

    resultados = estado.get("results") or []
    if not resultados or not resultados[0].get("location"):
        raise ErrorEtapa("render", "el render termino sin archivo", estado)
    st, _, png = http(resultados[0]["location"], "GET", None, {"Authorization": s["auth"]})
    if st != 200 or not png:
        raise ErrorEtapa("render", "no se pudo descargar la imagen generada (HTTP %s)" % st,
                         recortar(png))
    if len(png) > MAX_BYTES:
        raise ErrorEtapa("render", "la imagen supera %d MB" % (MAX_BYTES // 1024 // 1024))
    if png[:8] != b"\x89PNG\r\n\x1a\n":
        raise ErrorEtapa("render", "lo generado no es un PNG valido", recortar(png, 200))
    log("render OK: %d KB en %.1f s" % (len(png) // 1024, time.time() - inicio))
    return png, round(time.time() - inicio, 1)


# --------------------------------------------------------------------------
# Subida a Omarsa
# --------------------------------------------------------------------------
def subir_a_omarsa(contenido, nombre, mime, gw_key):
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


# --------------------------------------------------------------------------
# Servidor
# --------------------------------------------------------------------------
def dominio_descarga_permitido(url):
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return any(host == d.lstrip(".") or host.endswith(d) for d in DOMINIOS_DESCARGA)


class Handler(BaseHTTPRequestHandler):
    server_version = "SimuladorOmarsa/2.0"

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
            self._responder(200, {"ok": True, "servicio": "simulador omarsa",
                                  "rutas": ["POST /render-and-upload", "POST /upload-from-url"]})
        else:
            self._responder(404, {"error": True, "message": "ruta no encontrada"})

    def _autorizar(self):
        """Devuelve la key del gateway, o None si ya respondio con error."""
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
        largo = int(self.headers.get("Content-Length", "0") or 0)
        if largo > 1024 * 1024:
            raise ErrorPeticion("cuerpo demasiado grande")
        try:
            return json.loads(self.rfile.read(largo).decode("utf-8")) if largo else {}
        except Exception as e:
            raise ErrorPeticion("JSON invalido: %s" % e)

    def do_POST(self):
        ruta = self.path.split("?")[0].rstrip("/")
        if ruta not in ("/render-and-upload", "/upload-from-url"):
            return self._responder(404, {"error": True, "message": "ruta no encontrada"})
        gw_key = self._autorizar()
        if not gw_key:
            return
        try:
            datos = self._leer_json()
            if ruta == "/render-and-upload":
                return self._render_and_upload(datos, gw_key)
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

    def _render_and_upload(self, datos, gw_key):
        s = validar_solicitud(datos)
        png, segundos = renderizar(s)
        salida = subir_a_omarsa(png, s["nombre"], "image/png", gw_key)
        salida["render"] = {
            "tenant": s["tenant"], "appId": s["appId"], "objetoId": s["objeto"],
            "tipo": s["tipo"], "ancho": s["ancho"], "alto": s["alto"],
            "selecciones": s["aplicadas"], "archivo": s["nombre"],
            "bytes": len(png), "segundosRender": segundos,
        }
        return self._responder(200, salida)

    def _upload_from_url(self, datos, gw_key):
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
    puerto = int(os.environ.get("PORT", "10000"))
    if not os.environ.get("SIM_TOKEN", "").strip():
        log("AVISO: SIM_TOKEN no esta definido. El servicio respondera 500 a todo.")
    log("escuchando en el puerto %d" % puerto)
    ThreadingHTTPServer(("0.0.0.0", puerto), Handler).serve_forever()


if __name__ == "__main__":
    main()
