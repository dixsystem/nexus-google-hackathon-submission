#!/usr/bin/env bash
# Verificación rápida (~30s) de que el servicio Cloud Run sigue vivo y
# respondiendo antes de grabar el vídeo de submission. No requiere
# parámetros -- edita SERVICE_URL abajo si el servicio se redespliega bajo
# otro nombre/región/proyecto.
#
# ADVERTENCIA: las llamadas a /demo y /redteam de este script usan
# GEMINI real (mode real / mode=real) y gastan cuota real de
# Gemini/Gemma. rounds=2 en /redteam es deliberado para acotar ese gasto.
#
# NOTA (ver NIGHT_QUESTIONS.md, entrada "PASO 1 corrección (v2)" de
# 2026-08-22): /redteam SI esta desplegado y verificado en el servicio en
# vivo (confirmado con una llamada real exitosa, modo offline). Un HTTP 404
# en el paso de /redteam de abajo indicaria una regresion real, no un
# estado esperado -- investigalo.

set -eu -o pipefail

SERVICE_URL="https://nexus-google-agentic-demo-775963240525.us-central1.run.app"

# Formateador de JSON: usa `jq` si está instalado (salida más legible);
# si no, cae a `python3 -m json.tool`, que siempre está disponible porque
# el propio proyecto ya requiere Python 3.14 (ver Dockerfile). Se asume
# python3 -m json.tool como fallback documentado, no jq como requisito.
if command -v jq >/dev/null 2>&1; then
  format_json() { jq '.'; }
else
  format_json() { python3 -m json.tool; }
fi

RESULTS=()

# run_check <descripcion> <metodo> <path> [body-json]
run_check() {
  local description="$1"
  local method="$2"
  local path="$3"
  local body="${4:-}"
  local url="${SERVICE_URL}${path}"
  local response http_code body_out

  echo "== ${description} =="
  echo "   ${method} ${url}"

  if [ "$method" = "POST" ]; then
    if [ -n "$body" ]; then
      response=$(curl -s -w '\n%{http_code}' -X POST "$url" \
        -H 'Content-Type: application/json' -d "$body")
    else
      response=$(curl -s -w '\n%{http_code}' -X POST "$url")
    fi
  else
    response=$(curl -s -w '\n%{http_code}' "$url")
  fi

  http_code=$(printf '%s' "$response" | tail -n1)
  body_out=$(printf '%s' "$response" | sed '$d')

  echo "   HTTP status: ${http_code}"
  if [ -n "$body_out" ]; then
    printf '%s\n' "$body_out" | format_json 2>/dev/null || printf '%s\n' "$body_out"
  fi
  echo

  if [ "$http_code" -ge 200 ] && [ "$http_code" -lt 300 ]; then
    RESULTS+=("OK    ${description}")
  else
    RESULTS+=("FALLO ${description} (HTTP ${http_code})")
  fi
}

echo "Verificando servicio: ${SERVICE_URL}"
echo

run_check "Verificando health..." GET "/health"

run_check "Verificando demo real (gasta cuota real de Gemini)..." POST "/demo"

run_check "Verificando redteam real (mode=real, rounds=2 -- gasta cuota real de Gemini/Gemma)..." \
  POST "/redteam" '{"mode":"real","rounds":2}'

echo "== Resumen =="
for line in "${RESULTS[@]}"; do
  echo "  ${line}"
done
