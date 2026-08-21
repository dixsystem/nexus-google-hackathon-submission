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

## M-5 — origen de `gemini_assessment` y regla de desempate con `nexus_flagged`

**Contexto A (origen de gemini_assessment):** la misión decía "puedes
pedirle esto como parte del prompt de M-1 o como llamada separada,
documenta cuál elegiste". **Decisión:** llamada separada, NO añadida al
prompt de M-1. Razón: `red_team_attacker.py` (M-1, ya committeado) está
diseñado deliberadamente para devolver el intento crudo SIN que este
módulo interprete ni añada estructura sobre su propio ataque -- mezclar
"genera un ataque" con "autoevalúate" en el mismo prompt contamina la
independencia del juicio (el mismo modelo que generó el ataque no es un
evaluador independiente de sí mismo). `consensus_gate.py` recibe
`gemini_assessment` ya como valor calculado (uno de `SOPHISTICATED` /
`TRIVIAL` / `UNKNOWN`) -- el mecanismo real para poblarlo con una llamada
en vivo a Gemini queda fuera de esta misión (evitar más llamadas reales
de las estrictamente necesarias, regla dura de la sesión). `UNKNOWN` es
el valor seguro cuando esa llamada aún no existe o falla.

**Contexto B (regla de desempate no cubierta literalmente):** el texto
de la misión solo da dos ejemplos explícitos ("ESCALATE si los 3
coinciden", "ARCHIVE_LOW_INTEREST si Nexus marcó blocked pero los otros
dos lo ven trivial") -- no dice qué pasa cuando `nexus_flagged=False`
(Nexus NO bloqueó el intento) pero Gemini y Gemma sí coinciden en que es
interesante. Ese combo, con datos, no aparece explícitamente en el
enunciado.

**Decisión tomada (conservadora, favorece escalar antes que archivar en
silencio):** si Gemini y Gemma coinciden en que hay patrón real de
interés, el veredicto es ESCALATE sin importar `nexus_flagged` -- si
Nexus además lo bloqueó, hay acuerdo total; si Nexus NO lo bloqueó pero
los dos jueces de IA independientes sí lo consideran serio, es la señal
más crítica posible (un posible bypass real de la validación existente),
así que también escala. Si Gemini y Gemma coinciden en que es trivial,
el veredicto es ARCHIVE_LOW_INTEREST sin importar `nexus_flagged` (que
Nexus lo bloqueara solo significa que la regla determinista hizo su
trabajo sobre algo de bajo interés). NO_CONSENSUS cubre tanto la
discrepancia Gemini/Gemma como `gemini_assessment=UNKNOWN` (dato
faltante -- nunca se asume acuerdo sin la opinión real de Gemini).

**Por qué es segura:** `evaluate_consensus()` solo CLASIFICA datos ya
calculados -- no bloquea, aprueba ni ejecuta nada; el consensus más
"agresivo" (ESCALATE) solo implica notificar a un humano, nunca actuar.

**Pendiente de revisión humana:** confirmar si esta regla de desempate es
la deseada, o si se prefiere que `nexus_flagged=False` fuerce siempre
NO_CONSENSUS en vez de ESCALATE cuando Gemini/Gemma coinciden en interés.

---

## M-8 — model_id exacto de Lyria sin verificar; llamada real nunca invocada

**Contexto:** `grep -rni "lyria"` sobre todo el repo no arrojó ningún
resultado — cero precedente local. No tengo forma de verificar en vivo,
sin gastar cuota real, el nombre exacto de modelo ni el método SDK
correcto (`generate_content` vs. algún método específico de audio/música
del SDK `google-genai` instalado). Uso `DEFAULT_LYRIA_MODEL_ID =
"lyria-3-clip-preview"` como sugirió la propia misión, pero marcado
explícitamente como NO VERIFICADO en el código.

**Decisión tomada:** `generate_alert_sound()` exige un `transport`
inyectado (sin default real, misma disciplina que M-1/M-3); si
`transport=None` (el caso por defecto), la función se salta por completo
sin lanzar excepción -- devuelve `None` de inmediato, tratado como
"Lyria no está configurado", exactamente el modo fail-safe que pide la
misión. Ningún test ni código de este módulo invoca una API de Lyria
real en ningún momento de esta sesión.

**Pendiente de revisión humana:** confirmar el `model_id`/método SDK real
de Lyria antes de cablear un transport real en producción.

---
