# Simulador del endpoint `upload-from-url`

Implementación de referencia, funcionando, del endpoint que hay que pedirle a infraestructura de Omarsa. Sirve para dos cosas: probar el flujo completo desde Qlik antes de pedir nada, y entregarle a infraestructura una especificación con código que ya funciona en lugar de una descripción en palabras.

## El problema que resuelve

Qlik Automate **no puede armar una petición `multipart/form-data`**, así que no puede subir la imagen directamente al endpoint `api-upload/upload/file-temp` de Omarsa.

Pero **sí** puede hacer estas dos cosas, ambas verificadas:

- Subir la imagen a su propio almacenamiento temporal, con el bloque nativo *Upload File to Temporary Content*.
- Hacer POST con cuerpo JSON a un endpoint externo, con el bloque Call URL.

De ahí la idea: la automatización sube la imagen a `temp-contents` de Qlik, y le pasa a este servicio la URL de ese archivo más una API key de Qlik. El servicio la descarga y la vuelve a subir a `file-temp` de Omarsa, que sí es un storage público que WhatsApp/Omarsa pueden leer sin credenciales.

**Sobre si esto funciona:** al principio se pensó que no, porque un archivo de `temp-contents` subido por la conexión del conector de una automatización parecía no ser legible por una API key normal del mismo usuario (se recibía 404). Se comprobó más a fondo y **no era una restricción de identidad**: era que la API key usada en la prueba estaba vencida. Con una key vigente, la descarga funciona sin problema — confirmado el 21-sep-2026 con dos corridas seguidas del flujo completo. Es la ruta que usa hoy la automatización de producción.

## Contrato

```
POST /upload-from-url

Headers:
  Content-Type: application/json
  x-sim-token:  <token compartido>          # evita que sea un proxy abierto
  x-api-key:    <key dinámica del gateway>  # la que devuelve generate-app

Body:
  {
    "url": "https://<tenant>.qlikcloud.com/api/v1/temp-contents/<id>",
    "authorization": "Bearer <api key de Qlik>",
    "filename": "tablero.png",
    "ambiente": "produccion"   # opcional, ver mas abajo
  }

Respuesta 200:
  {
    "statusCode": 200,
    "message": "OK",
    "data": { "name": "<uuid>.png", "mimetype": "image/png", "file_code": null },
    "mediaUrl": "https://a1-api-view-lb.omarsa.com.ec/api-view/view/media-temp/<uuid>.png"
  }
```

La respuesta es la misma de `file-temp` más un campo `mediaUrl` ya armado, para que la automatización de Qlik no tenga que concatenar nada.

### Campo `ambiente` (produccion / test)

Desde el 22-sep-2026 el simulador acepta un campo opcional `ambiente` en el body, con dos valores posibles: `"produccion"` (endpoints `a1-api-upl-lb` / `a1-api-view-lb`) o `"test"` (endpoints `a3-api-upl` / `a3-api-view`). Si se omite, o viene con un valor distinto a esos dos, se usa `"produccion"` por defecto.

Esto existe porque, al 22-sep-2026, `a1-api-gw-ext-lb` (el gateway de producción) devuelve `403 Forbidden - Request forbidden by administrative rules` para peticiones externas — algo distinto a un 403 de la aplicación, parece un firewall/WAF que aún no tiene permitido el tráfico externo (`a1-api-upl-lb` y `a1-api-view-lb` sí responden bien; ver "Qué pedirle a infraestructura" más abajo). Mientras tanto, la automatización de Qlik puede seguir probando el flujo completo contra el ambiente de test (`a3`), que sigue funcionando de punta a punta. El cambio de ambiente se controla desde una sola variable en la automatización de Qlik (`vAmbiente`), que:

- decide la URL y el código que usa el bloque `generarKey` (gateway de `a1` o de `a3`), y
- se la pasa a este servicio en el campo `ambiente`, para que suba/arme la URL de vista contra el mismo ambiente que generó la key.

No hace falta ninguna variable de entorno ni redeploy para cambiar de ambiente: basta con editar el valor de `vAmbiente` en la automatización (`produccion` o `test`).

También responde `GET /health` con `{"ok": true}`.

### Errores

| HTTP | Causa |
|---|---|
| 400 | Falta `url`, no es https, o el JSON del cuerpo es inválido. |
| 401 | Falta el `x-sim-token` o es incorrecto. |
| 403 | El dominio de `url` no está en la lista blanca (`*.qlikcloud.com`, `*.omarsa.com.ec`). |
| 502, etapa `descarga` | No se pudo descargar el archivo (revisar la API key de Qlik en `authorization`, o que el archivo no haya caducado). |
| 502, etapa `subida` | `file-temp` de Omarsa rechazó el archivo (lo más común: la key del gateway venció). |

## Decisiones de diseño, y qué cambiaría en la versión definitiva

**Este simulador no guarda ninguna credencial de Omarsa.** La key del gateway se la pasa quien llama: Qlik la genera antes con `generate-app` y la manda en la cabecera. El endpoint definitivo, al vivir dentro de la red de Omarsa, probablemente la manejaría internamente y no la pediría — pero para una prueba es preferible que no queden secretos en un servicio de terceros.

**Solo descarga de dominios en lista blanca** (`*.qlikcloud.com` y `*.omarsa.com.ec`). Sin eso sería un descargador de URLs arbitrarias con credenciales de Omarsa detrás.

**El `x-sim-token` es obligatorio.** Sin él, cualquiera con la URL podría subir archivos al almacenamiento de Omarsa.

**Límite de 25 MB** por archivo, de sobra para un PNG de tablero (el real pesa unos 300 KB).

## Desplegarlo en Render

El servicio ya está creado: `omarsa-simulador-upload`, desplegado desde el repositorio `hectorxavi1988-gif/omarsa-simulador-upload`, rama `main`. Para actualizarlo, se reemplaza `app.py` en GitHub (*Edit* → pegar → *Commit*) y Render redespliega solo.

Configuración: runtime `python`, build `echo sin dependencias` (el script usa solo librería estándar), start `python app.py`, variable de entorno `SIM_TOKEN`, plan `free`.

Un detalle del plan gratuito: el servicio se duerme tras un rato sin uso y la primera petición puede tardar hasta un minuto en despertarlo. Para que eso no haga fallar la automatización de Qlik, conviene subir el *timeout* del bloque Call URL a 120-180 segundos.

## Correrlo con Docker (para infraestructura)

El repositorio incluye un `Dockerfile` para que infraestructura pueda construir y correr este mismo servicio donde le convenga (su propio Kubernetes, ECS, un VPS, etc.), sin depender de Render. `app.py` no tiene dependencias externas (solo librería estándar de Python), así que la imagen es mínima: no hay build de paquetes, solo se copia el archivo.

Construir la imagen:

```bash
docker build -t omarsa-simulador-upload .
```

Correrla:

```bash
docker run -d --name omarsa-simulador-upload \
  -p 10000:10000 \
  -e SIM_TOKEN="<token compartido>" \
  omarsa-simulador-upload
```

`SIM_TOKEN` es la única variable de entorno obligatoria (ver "Contrato" más arriba); **no va incluida en la imagen**, se inyecta en tiempo de ejecución, igual que en Render. `PORT` es opcional (por defecto 10000); si el orquestador de infraestructura asigna el puerto de otra forma, basta con pasar `-e PORT=<puerto>` y publicar ese puerto.

La imagen corre como usuario sin privilegios (no root) y trae un `HEALTHCHECK` contra `GET /health`.

**Importante para quien la despliegue:** el contenedor necesita salida a internet hacia `*.qlikcloud.com` (para descargar el archivo desde temp-contents) y hacia `a1-api-upl-lb.omarsa.com.ec` / `a1-api-view-lb.omarsa.com.ec` (para subirlo). Si infraestructura la corre detrás de un proxy saliente corporativo, hay que confirmar que esos dominios estén permitidos.

Para probarla en local con el `docker-compose.yml` incluido:

```bash
SIM_TOKEN="loquesea" docker compose up --build
curl http://localhost:10000/health
```

## Probarlo

```bash
# salud
curl https://omarsa-simulador-upload.onrender.com/health

# camino completo (con una key fresca del gateway y una URL de temp-contents vigente)
curl -X POST https://omarsa-simulador-upload.onrender.com/upload-from-url \
  -H "Content-Type: application/json" \
  -H "x-sim-token: <token>" \
  -H "x-api-key: <key del gateway>" \
  -d '{"url":"https://<tenant>.qlikcloud.com/api/v1/temp-contents/<id>",
       "authorization":"Bearer <api key de Qlik>",
       "filename":"tablero.png"}'
```

## Cómo queda la automatización de Qlik

Seis bloques, todos de tipos ya probados:

1. Selecciona los filtros (Año, Mes) y crea un bookmark.
2. `Get Chart Image` sobre el objeto/contenedor deseado, aplicando el bookmark.
3. `Upload File To Temporary Content` (Qlik Cloud Services), que sube la imagen a `temp-contents` y devuelve un `id`.
4. Call URL POST a `gateway-external/auth/generate-app`, que devuelve la key dinámica del gateway.
5. Call URL POST a este simulador, con la URL de `temp-contents` del paso 3 y la API key de Qlik, que devuelve `mediaUrl`.
6. Call URL POST a `send-template-async` con ese `mediaUrl`, que envía el WhatsApp.

Para enviar varias imágenes en una misma corrida se repite el bloque 5 (o se pone en un bucle sobre una lista de solicitudes); la key del gateway del paso 4 sirve para todas las llamadas de la misma corrida.

## Qué pedirle a infraestructura

Un endpoint con este mismo contrato, alojado por ellos. Dos observaciones que conviene transmitirles:

La primera es que tendrían que aceptar una API key de Qlik para hacer la descarga. Las keys de Qlik no se pueden acotar finamente, así que lo razonable es crearla desde un usuario de servicio con permisos mínimos sobre la app *Oferta y Demanda*, y renovarla antes de que caduque.

La segunda es que el contenido temporal de Qlik caduca en unas horas, así que la descarga tiene que ser inmediata: el endpoint no puede encolar el trabajo para procesarlo más tarde.

## Respaldo local: ruta alternativa `/render-and-upload`

Se probó también una segunda ruta, que en vez de recibir una URL de `temp-contents` recibe la *descripción* de la imagen (tenant, app, objeto, tamaño, filtros) y la genera ella misma con la Reports API de Qlik. Es más flexible — sirve para pedir imágenes de cualquier app u objeto sin que la automatización tenga que subir nada a `temp-contents` primero — pero no se está usando, así que **no está en el `app.py` desplegado ni en GitHub**.

Queda guardada en este mismo repositorio local, en `respaldo-local/app_render_and_upload.py`, con su propio contrato documentado en los comentarios del archivo. Si más adelante hace falta (por ejemplo, para publicar tableros de otra app), se reactiva copiando ese archivo sobre `app.py` y volviendo a pegarlo en GitHub.
