# DEPLOYMENT CHECKLIST — Red Team Cloud Run rollout

Checklist de referencia para la **sesión supervisada** de despliegue del
Red Team (`POST /redteam`, `GET /quarantine/<incident_id>`) sobre el
mismo servicio Cloud Run ya desplegado (`nexus-google-agentic-demo`).
Nada de este documento ejecuta ningún `gcloud` -- es preparación, no
despliegue. Generado durante PASO C de la sesión de preparación
2026-08-22 (ver `NIGHT_QUESTIONS.md` para el detalle de las decisiones
tomadas en PASO A y PASO B de esa misma sesión).

## 1. Variables de entorno

| Variable | Obligatoria para | Descripción | Valor esperado / ejemplo |
|---|---|---|---|
| `GEMINI_API_KEY` | `POST /demo` (modo real) | Credencial real de la API de Gemini. **No** la pases como `--set-env-vars` en texto plano si te importa que quede visible en `gcloud run services describe` / Cloud Console -- considera Secret Manager (`--set-secrets`) en vez de `--set-env-vars` si el proyecto ya tiene un secreto creado; el comando de referencia de abajo usa `--set-env-vars` porque es el patrón que el propio `README.md` del proyecto ya documenta y usa hoy en el servicio desplegado. | `AIza...` (nunca commitear el valor real) |
| `GEMINI_MODEL` | `POST /demo` (modo real) | Modelo de Gemini a usar para `/demo` real. `/redteam` **no** lo usa hoy -- corre siempre en modo offline/determinista (ver `NIGHT_QUESTIONS.md`, entrada M-7a). | `gemini-3.5-flash` (mismo modelo ya verificado en el servicio actual, ver `EVIDENCE_GEMINI_SMOKE_TEST_V1.md`) |
| `ENABLE_REAL_STORAGE` | `GET /quarantine/<id>` con persistencia real (opcional) | Flag añadido en esta sesión (PASO A). Si está ausente o tiene cualquier valor distinto de `"true"` (tolerante a mayúsculas/espacios), `QuarantineStore` usa el fallback in-memory de siempre -- los incidentes de cuarentena se pierden al reiniciar la instancia. Con `"true"`, construye un `google.cloud.storage.Client()` real. | `true` (solo si ya existe el bucket y el permiso del paso 2 de abajo) o **ausente** (comportamiento actual, sin cambios, recomendado para el primer despliegue de verificación) |
| `PORT` | Automática | La inyecta Cloud Run; no hace falta pasarla a mano (el Dockerfile ya expone 8080 y `main()` la lee de `PORT` con default `8080`). | (no pasar, gestionada por Cloud Run) |

## 2. Prerrequisitos si se activa `ENABLE_REAL_STORAGE=true`

`QuarantineStore` (ver `google_agentic_cloud_service.py`) usa
`storage_client.bucket(name)` -- **no** `create_bucket(name)` -- porque
el bucket de cuarentena es compartido entre sesiones, no uno por sesión
como en `mission_executor.py` (M-6). Eso significa que el bucket debe
**existir de antemano**; el servicio nunca lo crea.

Antes de desplegar con el flag activo:

1. Crear el bucket (nombre fijo, `DEFAULT_QUARANTINE_BUCKET_NAME` en el
   código): `gcloud storage buckets create gs://nexus-redteam-quarantine --project=<tu-proyecto> --location=us-central1`
2. Dar al service account que usa el servicio Cloud Run permiso de
   escritura/lectura sobre ese bucket (mínimo `roles/storage.objectAdmin`
   sobre el bucket, no a nivel de proyecto):
   `gcloud storage buckets add-iam-policy-binding gs://nexus-redteam-quarantine --member="serviceAccount:<SA-DEL-SERVICIO>@<tu-proyecto>.iam.gserviceaccount.com" --role="roles/storage.objectAdmin"`
3. Confirmar qué service account usa el servicio (si no se especificó
   `--service-account` en el deploy original, es el Compute Engine
   default: `<PROJECT_NUMBER>-compute@developer.gserviceaccount.com`):
   `gcloud run services describe nexus-google-agentic-demo --region=us-central1 --format="value(spec.template.spec.serviceAccountName)"`

Si se prefiere no exponer persistencia real todavía, **omitir
`ENABLE_REAL_STORAGE`** en el primer despliegue del Red Team -- el
servicio funciona igual, solo que la cuarentena no sobrevive a un
reinicio de instancia (comportamiento idéntico al de hoy, antes de esta
sesión).

## 3. Comando de referencia (NO ejecutar automáticamente)

Redeploy del servicio existente, con los endpoints del Red Team ya
incluidos en el Dockerfile (PASO B) y el flag de storage real activado.
Copiar/pegar y ajustar en la sesión supervisada:

```bash
export GEMINI_API_KEY=your-key-here

gcloud run deploy nexus-google-agentic-demo \
  --source=. \
  --region=us-central1 \
  --allow-unauthenticated \
  --set-env-vars=GEMINI_API_KEY=$GEMINI_API_KEY,GEMINI_MODEL=gemini-3.5-flash,ENABLE_REAL_STORAGE=true \
  --port=8080
```

Variante conservadora (Red Team desplegado, persistencia todavía
in-memory -- recomendada para la primera verificación en producción):

```bash
export GEMINI_API_KEY=your-key-here

gcloud run deploy nexus-google-agentic-demo \
  --source=. \
  --region=us-central1 \
  --allow-unauthenticated \
  --set-env-vars=GEMINI_API_KEY=$GEMINI_API_KEY,GEMINI_MODEL=gemini-3.5-flash \
  --port=8080
```

## 4. Verificación post-despliegue (referencia, no ejecutar aquí)

```bash
SERVICE_URL=$(gcloud run services describe nexus-google-agentic-demo --region=us-central1 --format="value(status.url)")

curl -sf "$SERVICE_URL/health"
curl -sf -X POST "$SERVICE_URL/redteam" -d '{"rounds":1}'
# Si ENABLE_REAL_STORAGE=true y el /redteam anterior escaló algún incidente,
# el incident_id aparece en escalated_incident_ids de la respuesta:
curl -sf "$SERVICE_URL/quarantine/<incident_id>"
```

`POST /redteam` corre siempre en modo offline/determinista hoy (ver
`NIGHT_QUESTIONS.md`, M-7a) -- cada ronda se reporta como
`VALIDATION_BYPASS`, nunca se ejecuta nada, y normalmente
`escalated_incident_ids` queda vacío (no hay nada que consultar en
`/quarantine` salvo que se fuerce un escenario `ESCALATE` explícitamente,
como hacen los tests).

## 5. Fuera de alcance de este checklist

- Cablear un modo `/redteam` "real" contra Gemini vivo (pendiente,
  documentado en `NIGHT_QUESTIONS.md`, entrada M-7a).
- Implementar `gemini_assessor` real en vez de `"UNKNOWN"` por defecto
  (pendiente, documentado en `SESSION_SUMMARY_2026-08-22.md`).
- Migrar `GEMINI_API_KEY` de `--set-env-vars` a Secret Manager
  (`--set-secrets`) -- el comando de referencia arriba sigue el patrón ya
  usado por el servicio desplegado hoy, no introduce uno nuevo.
