# NIGHT SESSION — Preguntas y decisiones de diseño documentadas

Cada entrada documenta una decisión tomada de forma autónoma (conservadora,
sin gastar cuota real, sin tocar Cloud/gcloud/push) cuando la misión no
especificaba el detalle exacto. Ninguna de estas decisiones activa
ejecución real ni gobernanza nueva — quedan sujetas a revisión humana.

---

## M-2 — No existe un patrón literal de "hash-chaining" en el repo

**Contexto:** la misión pedía "leer mission_proposal_staging.py para copiar
exactamente el patrón de hash-chaining que ya usa el proyecto". Revisé
`mission_proposal_staging.py` completo: usa sha256 de **documentos**
(`proposals_sha256`, `contracts_sha256`) solo para detectar *staleness*
(TOCTOU) contra el disco — no encadena un registro con el anterior.
El único precedente de hash *derivado de contenido con dominio separado*
es `mission_generator_llm_producer._generation_id` (dominio fijo +
JSON canónico + sha256).

**Decisión tomada:** en `red_team_incident.py` implementé un encadenado
real: `incident_hash = sha256(DOMAIN + canonical_json(campos) +
previous_incident_hash)`, con una constante `GENESIS_INCIDENT_HASH` fija
(sha256 de un dominio literal) para el primer incidente de cada sesión —
extiende el único patrón de hash con dominio separado que sí existe en el
repo (`_generation_id`), en vez de inventar un esquema sin precedente.

**Por qué es segura:** es una decisión de implementación local (formato de
hash de un registro de auditoría de solo lectura), no una decisión de
gobernanza — no afecta ninguna ruta de aprobación/ejecución existente.

**Pendiente de revisión humana:** confirmar que este esquema de
encadenado es el que se quiere usar si en el futuro se integra con algún
almacén de auditoría existente (ninguno existe hoy en este repo).

---

## M-3 — model_id exacto de Gemma sin verificar; classify() real diferido

**Contexto:** `grep -rni "gemma"` sobre todo el repo (código, requirements,
docs) no arrojó ningún resultado — cero precedente de qué model_id usar
para Gemma vía el SDK `google-genai` ya instalado
(`google-genai==2.18.1`, ver `antigravity_google_genai_backend.py`). No
tengo forma de verificar en vivo, sin gastar cuota real, qué cadena exacta
acepta `client.models.generate_content(model=...)` para un modelo Gemma
(candidatas plausibles por conocimiento general no verificado: algo del
estilo `gemma-3-27b-it` / `gemma-3n-e4b-it`, pero NINGUNA se hardcodea en
el código).

**Decisión tomada:** `GemmaSeverityClassifier` exige `model_id` explícito
en el constructor (sin default), exactamente la misma disciplina que ya
usa `AntigravityGeminiConfig` para Gemini ("Sin default deliberado" — ver
docstring de `antigravity_gemini_provider.py`). El camino real
(`classify()` con un transport inyectado real) queda estructuralmente
listo pero nunca invocado en producción esta noche. Como camino
**garantizado disponible y por defecto seguro**, implementé
`classify_with_fallback_rules()`: un clasificador 100% determinista,
basado en reglas, sin red — claramente marcado como FALLBACK tanto en el
código (`source="FALLBACK_RULES"` en `SeverityAssessment`) como aquí.

**Pendiente de revisión humana:** confirmar el `model_id` real de Gemma
disponible vía Gemini Developer API antes de cablear `classify()` a un
transport real en producción.

---
