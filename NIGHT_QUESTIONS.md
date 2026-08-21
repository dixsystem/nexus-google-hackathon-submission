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
