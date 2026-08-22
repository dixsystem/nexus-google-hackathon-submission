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

## M-3 — model_id exacto de Gemma sin verificar; classify() real diferido — **RESUELTA**

**RESUELTO (revisión humana, sesión posterior a la noche del 2026-08-21):**
model_id confirmado por documentación oficial de Google
(ai.google.dev/gemma): `GEMMA_MODEL_ID = "gemma-4-26b-a4b-it"`. Se llama
exactamente igual que Gemini, mismo SDK `google-genai`, mismo método
`client.models.generate_content(model=..., contents=...)`. Ahora es el
default de `GemmaSeverityClassifier.model_id` (código actualizado); el
fallback determinista `classify_with_fallback_rules()` se mantiene
disponible para cuando no se quiera gastar cuota real. Test añadido:
`test_default_model_id_is_the_verified_gemma_constant`.

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

## M-5 — origen de `gemini_assessment` y regla de desempate con `nexus_flagged` — **Contexto B RESUELTA**

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

**RESUELTO (revisión humana, sesión posterior a la noche del 2026-08-21):**
confirmado -- `nexus_flagged=False` con Gemini y Gemma coincidiendo en
interés real produce ESCALATE, no ARCHIVE_LOW_INTEREST ni NO_CONSENSUS.
Razón explícita del revisor: si dos evaluadores independientes detectan
algo que la regla determinista de Nexus no cazó, ese es precisamente el
escenario de MAYOR interés para revisión humana, no uno de menor
prioridad. El comportamiento de `evaluate_consensus()` ya era este por
diseño original (decisión conservadora de esa misma noche); esta
revisión lo confirma como regla intencional, hace la rama explícita en
el código (antes era un efecto derivado de la lógica general, no un
`elif` nombrado) y añade
`test_human_reviewed_nexus_not_flagged_ai_agreement_escalates_not_archived`
como cobertura dedicada. Contexto A (llamada separada para
gemini_assessment) permanece sin cambios -- no fue parte de esta
revisión.

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

## 2026-08-22 — M-9/M-7a: reordenado, firma de run_red_team_session, y origen de gemini_assessment

**Contexto A (reordenado M-7a/M-9):** la tarea pedía ejecutar "M-7a, M-9,
M-10, M-11" en ese orden, pero M-7a dice explícitamente "reutilizando el
orquestador que construirás en M-9" -- M-7a depende de un módulo que
todavía no existe si se sigue el orden literal. **Decisión:** construí
M-9 primero (el orquestador), luego M-7a (los endpoints HTTP que lo
llaman). Es la única secuencia que no requiere escribir código contra un
módulo inexistente.

**Contexto B (firma de run_red_team_session):** la misión especifica
`run_red_team_session(goal, registry, transport, rounds=5,
session_id=None)` -- sin ningún parámetro de model_id para el atacante
Gemini. `RedTeamAttacker` (M-1) exige `model_id` explícito sin default
("sin default deliberado", misma disciplina que `AntigravityGeminiConfig`)
-- inventar un model_id de Gemini aquí para no romper la firma literal
violaría esa disciplina ya establecida en toda la sesión. **Decisión:**
añadí `model_id` como parámetro keyword-only OBLIGATORIO (sin default),
extendiendo la firma en vez de violarla; los primeros 5 parámetros
posicionales coinciden exactamente con el enunciado.

**Contexto C (origen de gemini_assessment dentro del orquestador):** M-5
(anoche) ya documentó que `gemini_assessment` viene de "una llamada
SEPARADA a Gemini" pero que esa llamada real "queda fuera de esta
misión". M-9 SÍ necesita alimentar `evaluate_consensus()` con algo, y el
enunciado de M-9 no aclara si esa llamada separada debe implementarse
ahora. Regla dura de la sesión: "NO gastes cuota real... salvo lo
estrictamente necesario". **Decisión:** por defecto,
`run_red_team_session()` NUNCA hace esa tercera llamada real -- usa
`gemini_assessment="UNKNOWN"` para todo incidente (lo cual, por la regla
ya resuelta de M-5, siempre produce NO_CONSENSUS a menos que se inyecte
otra cosa). Añadí un parámetro opcional `gemini_assessor` (callable
inyectable, `Callable[[RedTeamIncident], str]`) para que un llamador que
sí quiera esa evaluación real (o un test que quiera forzar un escenario
ESCALATE) pueda suministrarla explícitamente, sin que el módulo la fuerce
por defecto.

**Por qué es segura:** ninguna de las tres decisiones afecta gobernanza o
ejecución -- son decisiones de firma/composición de módulos ya
existentes y no modificados. `run_red_team_session()` nunca invoca
`mission_executor.py` (M-6): un intento que pasa TODA la validación se
marca como `VALIDATION_BYPASS` y se reporta, nunca se ejecuta
automáticamente -- ver docstring de `red_team_session.py`.

**Pendiente de revisión humana:** confirmar el `model_id` real de Gemini
a usar en producción para el atacante (mismo pendiente ya documentado
para Gemma/Lyria), y si se desea implementar la llamada real de
`gemini_assessor` antes de exponer `/redteam` en producción.

---

## 2026-08-22 — M-7a: POST /redteam corre siempre en modo offline/determinista

**Contexto:** la misión no especifica si `/redteam` debe operar en modo
"real" (Gemini vivo, vía el mismo `IsolatedGeminiTransport` que ya usa
`/demo`) o en el modo offline determinista que ya usa `/demo/offline`.
Un modo "real" completo necesitaría DOS transports aislados por ronda
(uno para el atacante Gemini, otro para Gemma) -- el doble de la
complejidad de proceso-hijo que `/demo` ya maneja para uno solo -- y esta
misión es explícitamente "código only, SIN desplegar": no hay forma de
probar un modo real contra credenciales reales sin gastar cuota, ni
sentido en cablear esa complejidad antes de la sesión de despliegue
supervisada.

**Decisión tomada:** `POST /redteam` usa siempre
`build_transport("offline", ...)` (el mismo backend determinista local
ya usado por `/demo/offline`, sin red real) para el atacante, y
`use_gemma_fallback=True` en `run_red_team_session()` para evitar
necesitar un segundo transport de Gemma por completo. El backend offline
existente (`google_agentic_demo._offline_candidate_json()`) devuelve
siempre un candidato legítimo fijo sin importar el prompt -- así que en
este modo, cada ronda del `/redteam` offline se marca como
`VALIDATION_BYPASS` de forma determinista (nunca ejecutado, solo
reportado): es una demostración honesta del cableado completo M-1..M-6,
no un red-team real. Un modo `/redteam` real (Gemini vivo) queda
explícitamente para la sesión supervisada de despliegue.

**Por qué es segura:** no hay llamada de red real, no hay gasto de cuota,
y el resultado (siempre VALIDATION_BYPASS, nunca ejecutado) no puede
disparar ningún efecto -- `run_red_team_session` ya garantiza que un
VALIDATION_BYPASS nunca invoca `mission_executor.py`.

**Pendiente de revisión humana:** decidir si vale la pena cablear un modo
real para `/redteam` en la sesión de despliegue, y con qué política de
límite de cuota.

---

## 2026-08-22 — PASO A (preparación de despliegue): cliente real de Cloud Storage detrás de ENABLE_REAL_STORAGE

**Contexto:** la misión pedía conectar un `storage.Client()` real en
`google_agentic_cloud_service.py`, activo solo con una variable de
entorno explícita (nunca por defecto), y "aplicar el mismo patrón a
`mission_executor.py` si no está ya conectado igual (revisa M-6
primero)".

**Decisión tomada (`google_agentic_cloud_service.py`):** se añadió
`_build_storage_client(environ=None, *, storage_module=None)`. Devuelve
`None` a menos que `ENABLE_REAL_STORAGE` esté presente en el entorno con
valor `"true"` (tolerante a mayúsculas/minúsculas y espacios alrededor,
p.ej. `"True"`, `" true"`; cualquier otro valor -- `"1"`, `"yes"`, vacío
-- se trata como ausente). Solo en ese caso importa `google.cloud.storage`
de forma perezosa (dentro de la función, nunca a nivel de módulo) y
construye `storage.Client()`. El singleton módulo-nivel pasó de
`QuarantineStore()` a `QuarantineStore(_build_storage_client())` -- sin
la variable, el resultado es exactamente el mismo `None` de antes, así
que el fallback in-memory no cambia. `storage_module=` es un punto de
inyección explícito para tests (mismo estilo de seam que
`storage_client` en `MissionExecutor`/`QuarantineStore`), en vez de
parchear `sys.modules` -- evita depender de que el paquete
`google-cloud-storage` esté instalado para poder testear la rama "flag
activo" (no lo está en este entorno de desarrollo).

**Decisión tomada (`mission_executor.py`): SIN CAMBIOS, deliberadamente.**
Revisé M-6 primero, como pedía la instrucción: `MissionExecutor.__init__`
ya exige `storage_client` explícito y lanza `MissionExecutorError` si es
`None` -- nunca tuvo (ni debía tener) un default real que envolver detrás
de un flag, esa es precisamente su disciplina ya documentada ("sin
default real", ver docstring del módulo). Además, confirmé por grep que
ningún módulo del repo fuera de tests instancia `MissionExecutor(...)`
todavía -- no existe un entrypoint de producción propio donde insertar la
construcción del cliente real. Añadir un `_build_storage_client()` sin un
punto de uso real habría sido código muerto. Cuando exista un llamador
real de `MissionExecutor` (fuera de esta sesión), ese llamador es quien
debe inyectar el `storage.Client()` real -- el mismo
`_build_storage_client()` de `google_agentic_cloud_service.py` sirve
directamente para eso si se reutiliza tal cual.

**Verificación:** suite `google-all-things-agentic-submission/cloud/`
(`PYTHONPATH=engineering-loop python3 -m unittest
test_google_agentic_cloud_service -v`) -- 32 tests, todos en verde (14
nuevos: `BuildStorageClientTests`, cubriendo flag ausente, valores no
exactamente `"true"`, tolerancia a mayúsculas/espacios, y construcción
real del cliente fake sin red). Suite completa `engineering-loop/`
(`python3 -m unittest discover tests -v`) -- 366 tests, mismos 2 errores
preexistentes de antes de esta sesión (no relacionados, documentados en
`SESSION_SUMMARY_2026-08-22.md`), sin regresiones nuevas.

**Pendiente de revisión humana:** decidir, en la sesión de despliegue
supervisada, si `ENABLE_REAL_STORAGE=true` se activa desde el primer
`gcloud run deploy` o se deja para una iteración posterior tras verificar
el servicio en modo in-memory primero.

---
