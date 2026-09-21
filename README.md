# Simulador del endpoint de subida de imágenes

Esta es una implementación de referencia que ya funciona. Reproduce el endpoint que hay que pedirle a infraestructura de Omarsa. Tiene dos rutas — una en uso hoy, otra como alternativa — y las dos sirven para dos cosas: probar el flujo completo desde Qlik antes de pedir nada, y entregarle a infraestructura una especificación con código probado en lugar de una descripción en palabras.

## El problema que resuelve

Qlik Automate no puede armar una petición `multipart/form-data`, así que no puede subir la imagen directamente al endpoint `api-upload/upload/file-temp` de Omarsa. Lo que sí puede hacer es un POST con cuerpo JSON (bloque Call URL) y subir un archivo a su propio almacenamiento temporal (`temp-contents`, con el bloque nativo *Upload File to Temporary Content*).

De ahí las dos rutas de este servicio:

## Ruta en uso: `/upload-from-url`

La automatización sube la imagen a `temp-contents` de Qlik, y le pasa a este servicio la URL de ese archivo más una API key de Qlik. El servicio la descarga y la vuelve a subir a `file-temp` de Omarsa.

```
POST /upload-from-url
Headers:
  Content-Type: application/json
  x-sim-token:  <token compartido>
  x-api-key:    <key dinámica del gateway>
Body:
  {
    "url": "https://<tenant>/api/v1/temp-contents/<id>",
    "authorization": "Bearer <api key de Qlik>",
    "filename": "tablero.png"
  }
```

**Sobre si esto funciona:** al principio se pensó que no, porque un archivo de `temp-contents` subido por la conexión del conector de una automatización parecía no ser legible por una API key normal del mismo usuario (se recibía 404). Se comprobó más a fondo y **no era una restricción de identidad**: era que la API key usada en la prueba estaba vencida. Con una key vigente, la descarga funciona sin problema — confirmado el 2026-09-21 con dos corridas seguidas del flujo completo. Es la ruta que usa hoy la automatización de producción.

## Ruta alternativa: `/render-and-upload`

Para cuando se necesite pedir imágenes de otras apps u objetos sin que la automatización tenga que subir nada a `temp-contents` primero. En vez de una URL, la automatización manda la *descripción* de la imagen — qué app, qué objeto, de qué tamaño, con qué filtros — y el servicio la genera él mismo con la Reports API de Qlik antes de subirla.

```
POST /render-and-upload
Headers:
  Content-Type: application/json
  x-sim-token:  <token compartido>          # evita que sea un proxy abierto
  x-api-key:    <key dinámica del gateway>  # la que devuelve generate-app
```

### Cuerpo

```json
{
  "qlik": {
    "tenant": "omarsa.us.qlikcloud.com",
    "authorization": "Bearer <api key de Qlik>"
  },
  "imagen": {
    "appId": "a68d2b05-46a4-4775-82e4-1617e56445b1",
    "objetoId": "MLjEqp",
    "tipo": "visualization",
    "ancho": 1800,
    "alto": 410,
    "dpi": 96,
    "zoom": 1,
    "estrategiaSeleccion": "failOnErrors",
    "selecciones": [
      { "campo": "Año", "valores": ["{{anio:+1}}"] },
      { "campo": "Mes", "valores": ["{{mes:+1}}"] }
    ]
  },
  "archivo": {
    "nombre": "demanda-{{anio:+1}}-{{mesnum2:+1}}.png"
  }
}
```

### Campos

| Campo | Oblig. | Por defecto | Qué es |
|---|---|---|---|
| `qlik.tenant` | sí | — | Host del tenant. Solo se aceptan hosts `*.qlikcloud.com`. Da igual si trae o no `https://`. |
| `qlik.authorization` | sí | — | API key de Qlik. Si no empieza con `Bearer `, se le agrega. |
| `imagen.appId` | sí | — | Id de la app de Qlik. |
| `imagen.objetoId` | sí | — | Id del objeto (gráfico, tabla, contenedor) o de la hoja. |
| `imagen.tipo` | no | `visualization` | `visualization` para un objeto, `sheet` para una hoja completa. |
| `imagen.ancho` / `imagen.alto` | no | 1280 × 720 | Lienzo en píxeles (100 a 6000). |
| `imagen.dpi` | no | 96 | 72 a 300. |
| `imagen.zoom` | no | 1 | 0.25 a 4. |
| `imagen.estrategiaSeleccion` | no | `failOnErrors` | `failOnErrors` falla si un filtro no existe. `ignoreErrorsReturnDetails` y `ignoreErrorsNoDetails` lo ignoran. |
| `imagen.selecciones` | no | sin filtros | Lista de filtros; su formato se explica debajo. |
| `imagen.selectionsByState` | no | — | Modo avanzado: se pasa tal cual a la Reports API y reemplaza a `selecciones`. |
| `archivo.nombre` | no | `qlik-<objeto>-<fecha>.png` | Nombre del PNG que se sube. Acepta marcadores de período. |

**Formato de cada selección:**

```json
{ "campo": "Mes", "valores": ["octubre", "noviembre"], "numerico": false, "estado": "$" }
```

- `campo`: el nombre exacto del campo en el modelo de la app, incluidas tildes y mayúsculas.
- `valores`: uno o varios valores; varios equivalen a seleccionar varios a la vez.
- `numerico`: `true` si el campo se cargó como número. Por defecto es `false`, que selecciona por texto.
- `estado`: el estado alterno. Casi siempre se omite, y el valor por defecto es `$`, el estado principal.

### Marcadores de período

Los valores de las selecciones y el nombre del archivo aceptan marcadores. El servicio los calcula con la fecha de Ecuador en el momento de la llamada. Así Qlik no tiene que armar fechas con fórmulas.

| Marcador | Resultado (hoy = 21-sep-2026) |
|---|---|
| `{{anio}}` / `{{anio:+1}}` | `2026` / `2026` (el año del mes desplazado) |
| `{{mes}}` / `{{mes:+1}}` | `septiembre` / `octubre` |
| `{{Mes:+1}}` / `{{MES:+1}}` | `Octubre` / `OCTUBRE` |
| `{{mesnum:+1}}` / `{{mesnum2:-1}}` | `10` / `08` |
| `{{hoy}}` / `{{hoy:-1}}` | `2026-09-21` / `2026-09-20` (este desplaza **días**) |

El desplazamiento de `anio`, `mes` y `mesnum` es en **meses** y maneja el cambio de año. Por ejemplo, en diciembre, `{{mes:+1}}` da `enero` y `{{anio:+1}}` da el año siguiente. Por eso, para "el mes siguiente" se usa el mismo `+1` en año y mes.

### Respuesta 200

```json
{
  "statusCode": 200,
  "message": "OK",
  "data": { "name": "<uuid>.png", "mimetype": "image/png", "file_code": null },
  "mediaUrl": "https://a3-api-view.omarsa.com.ec/api-view/view/media-temp/<uuid>.png",
  "render": {
    "tenant": "omarsa.us.qlikcloud.com",
    "appId": "a68d2b05-...",
    "objetoId": "MLjEqp",
    "tipo": "visualization",
    "ancho": 1800,
    "alto": 410,
    "selecciones": [
      { "campo": "Año", "estado": "$", "valores": ["2026"] },
      { "campo": "Mes", "estado": "$", "valores": ["octubre"] }
    ],
    "archivo": "demanda-2026-10.png",
    "bytes": 315330,
    "segundosRender": 8.1
  }
}
```

Las primeras tres claves son idénticas a las de `file-temp`. `mediaUrl` viene ya armado. `render` repite lo que se aplicó de verdad, con los marcadores ya resueltos, para poder auditar qué período salió.

### Errores

Todos los errores tienen la misma forma: `{"error": true, "etapa": "...", "message": "...", "detalle": ...}`. La clave `etapa` dice dónde falló.

| HTTP | etapa | Causa típica |
|---|---|---|
| 400 | `validacion` | Falta un campo, un valor está fuera de rango o el JSON es inválido. |
| 401 | — | Falta el `x-sim-token` o es incorrecto. |
| 403 | `validacion` | El tenant no es `*.qlikcloud.com`. |
| 502 | `render` | La API key de Qlik es inválida, el objeto no existe, **la hoja no está publicada** o un filtro no existe. `detalle.pista` sugiere la causa. |
| 502 | `subida` | `file-temp` rechazó el archivo. Lo más común es que la key del gateway esté vencida. |

## Requisitos del lado de Qlik

Dos requisitos, ya comprobados con el tablero actual:

- **La hoja debe estar publicada.** La Reports API no ve hojas privadas y responde `chart not found` / `empty GenericType`.
- **La API key debe tener acceso a la app.** Hereda los permisos del usuario que la creó.

Para otra app u otro gráfico basta con cambiar `appId`, `objetoId`, el tamaño y los filtros. El `objetoId` se ve en la app: clic derecho sobre el objeto → *Compartir* → *Insertar*, o con *Developer* activado.

## Cómo queda la automatización de Qlik

**Con la ruta en uso (`/upload-from-url`)**, la que corre hoy en producción:

1. Selecciona los filtros (Año, Mes) y crea un bookmark.
2. `Get Chart Image` sobre el objeto/contenedor deseado, aplicando el bookmark.
3. `Upload File To Temporary Content` (bloque nativo de Qlik Cloud Services), que sube esa imagen a `temp-contents` y devuelve un `id`.
4. Call URL POST a `gateway-external/auth/generate-app`, que devuelve la key del gateway.
5. Call URL POST a `/upload-from-url`, con la URL de `temp-contents` del paso 3 y la API key de Qlik, que devuelve `mediaUrl`.
6. Call URL POST a `send-template-async` con ese `mediaUrl`, que envía el WhatsApp.

**Con la ruta alternativa (`/render-and-upload`)** el flujo es más corto porque Qlik no necesita subir nada a `temp-contents` primero:

1. Call URL POST a `generate-app`, que devuelve la key del gateway.
2. Call URL POST a `/render-and-upload` con el cuerpo de arriba (app, objeto, filtros) y la key en `x-api-key`, que devuelve `mediaUrl`.
3. Call URL POST a `send-template-async` con ese `mediaUrl`.

Para enviar varias imágenes en una misma corrida, sean de la misma app o de apps distintas, se repite el bloque de subida (5 o 2, según la ruta), o se pone en un bucle sobre una lista de solicitudes. La key del gateway sirve para todas las llamadas de la misma corrida.

En el bloque que llama al simulador conviene poner un *timeout* de 120 a 180 segundos: el trabajo real toma pocos segundos, pero el plan gratuito de Render duerme el servicio tras un rato sin uso, y la primera llamada puede tardar hasta un minuto en despertarlo.

## Desplegarlo en Render

El servicio ya está creado: `omarsa-simulador-upload`, que se despliega desde `hectorxavi1988-gif/omarsa-simulador-upload`, rama `main`. Para actualizarlo, se reemplaza `app.py` en GitHub (*Edit* → pegar → *Commit*). Render redespliega solo, o lo redespliego yo.

Configuración: runtime `python`, build `echo sin dependencias` (solo usa la librería estándar), start `python app.py`, variable `SIM_TOKEN`.

## Probarlo con curl

```bash
curl https://omarsa-simulador-upload.onrender.com/health

curl -X POST https://omarsa-simulador-upload.onrender.com/render-and-upload \
  -H "Content-Type: application/json" \
  -H "x-sim-token: <token>" \
  -H "x-api-key: <key del gateway>" \
  -d '{"qlik":{"tenant":"omarsa.us.qlikcloud.com","authorization":"Bearer <api key de Qlik>"},
       "imagen":{"appId":"a68d2b05-46a4-4775-82e4-1617e56445b1","objetoId":"MLjEqp",
                 "ancho":1800,"alto":410,
                 "selecciones":[{"campo":"Año","valores":["{{anio:+1}}"]},
                                {"campo":"Mes","valores":["{{mes:+1}}"]}]},
       "archivo":{"nombre":"demanda-{{anio:+1}}-{{mesnum2:+1}}.png"}}'
```

## Qué pedirle a infraestructura

Un endpoint con este mismo contrato, alojado por ellos. Conviene transmitirles tres cosas:

1. **Credenciales dentro de su red.** En la versión definitiva, lo razonable es que el servicio guarde la API key de Qlik y el `code` del gateway en su propio almacén de secretos. Así la automatización no manda credenciales en el cuerpo, y `qlik.authorization` y el header `x-api-key` desaparecen del contrato. En el simulador viajan en la petición solo para no dejar secretos en un servicio de terceros.
2. **Usuario de servicio para Qlik.** La API key de Qlik conviene crearla desde un usuario de servicio con acceso de solo lectura a las apps que se van a publicar por WhatsApp. Y los tenants permitidos conviene fijarlos a `omarsa.us.qlikcloud.com`.
3. **Llamada síncrona.** La llamada espera a que Qlik termine de generar la imagen, normalmente en menos de 20 segundos. El tope configurado es de 150 segundos. Si prefieren un modelo asíncrono (202 + consulta de estado), Qlik Automate lo puede manejar con un bucle de espera, pero el síncrono es más simple de consumir.
