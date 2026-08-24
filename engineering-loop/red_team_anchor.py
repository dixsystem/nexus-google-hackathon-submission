"""External Proof Anchor (M-9 follow-up): ancla el Merkle root de una
sesión red-team ESCALADA fuera del dominio de confianza de NEXUS/GCP,
mediante un commit a un repo público de GitHub
(dixsystem/nexus-agentic-proof-anchor, decisión del operador -- ver PART 3).

Disparo: SIEMPRE manual, ejecutado por el operador (`python
red_team_anchor.py anchor <session_id>`) usando las credenciales `gh` ya
autenticadas en esta máquina. El servicio Cloud Run NUNCA recibe ni
necesita un token de GitHub -- ver docstring de main() y la decisión
explícita del operador (AskUserQuestion, sesión External Proof Anchor):
"Script/CLI manual... El propio Cloud Run NUNCA necesita credenciales de
GitHub".

Todo el flujo de verify (`python red_team_anchor.py verify <session_id>`)
es alcanzable por un juez sin ninguna credencial: el JSON completo de la
sesión se lee del endpoint público GET /quarantine/session-<id> (ya
desplegado, --allow-unauthenticated), y el registro anclado se lee de
raw.githubusercontent.com (repo público, sin auth). No reinventa hashing:
reusa incident.incident_hash (red_team_incident.build_incident, ya
producido) como hojas, y red_team_merkle.merkle_root/verify_merkle (ya
implementados, PART 1) para combinarlas/compararlas."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import re
import subprocess
import urllib.error
import urllib.request

import red_team_incident
import red_team_merkle


DEFAULT_CLOUD_RUN_BASE_URL = "https://nexus-google-agentic-demo-775963240525.us-central1.run.app"
DEFAULT_ANCHOR_REPO = "dixsystem/nexus-agentic-proof-anchor"
DEFAULT_ANCHOR_BRANCH = "main"
_SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


class AnchorError(Exception):
    """Cualquier fallo de este módulo: red, GCS, git, o datos malformados
    -- nunca una excepción sin categorizar cruza al CLI."""


def _require_session_id(session_id: str) -> str:
    if not isinstance(session_id, str) or _SESSION_ID_PATTERN.fullmatch(session_id) is None:
        raise AnchorError(f"invalid session_id: {session_id!r}")
    return session_id


def fetch_session_document(session_id: str, *, base_url: str = DEFAULT_CLOUD_RUN_BASE_URL) -> dict:
    """Descarga el JSON completo de la sesión desde el endpoint público
    GET /quarantine/session-<session_id> (mismo QuarantineStore.put() que
    ya persiste esto -- ver google_agentic_cloud_service.py, PART 2). Solo
    existe si la sesión escaló (única condición bajo la que PART 2
    persiste el JSON completo)."""

    session_id = _require_session_id(session_id)
    url = f"{base_url}/quarantine/session-{session_id}"
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        raise AnchorError(f"failed to fetch session document: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("status") != "FOUND":
        raise AnchorError(
            f"session {session_id!r} not found -- did it escalate (PART 2 only persists ESCALATE sessions)?"
        )
    try:
        return json.loads(payload["quarantine_report"])
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise AnchorError(f"stored session document is not valid JSON: {exc}") from exc


def _leaf_hashes(session_doc: dict) -> list[str]:
    try:
        return [incident["incident_hash"] for incident in session_doc["incidents"]]
    except (KeyError, TypeError) as exc:
        raise AnchorError(f"session document missing incidents/incident_hash: {exc}") from exc


def build_anchor_record(session_doc: dict, *, clock=None) -> dict:
    """leaf_hashes se publica JUNTO al merkle_root (no solo el root) --
    decisión deliberada: el root por sí solo es la forma compacta correcta
    de anclar, pero sin las hojas originales publicadas en algún lugar,
    NINGÚN verificador (ver red_team_merkle.verify_merkle) puede localizar
    cuál hoja cambió tras un tamper -- solo puede confirmar que algo
    cambió. Publicar las hojas también en el anchor (~15 hashes por
    sesión como máximo, MAX_REDTEAM_ROUNDS) resuelve esto sin costo
    relevante."""

    leaves = _leaf_hashes(session_doc)
    resolved_clock = clock if clock is not None else (lambda: datetime.now(timezone.utc))
    return {
        "session_id": session_doc["session_id"],
        "merkle_root": red_team_merkle.merkle_root(leaves),
        "leaf_hashes": leaves,
        "incident_count": len(leaves),
        "anchored_at": resolved_clock().isoformat(),
    }


def _run_git(args: list[str], *, cwd: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        raise AnchorError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def anchor_session(
    session_id: str,
    *,
    base_url: str = DEFAULT_CLOUD_RUN_BASE_URL,
    repo: str = DEFAULT_ANCHOR_REPO,
    branch: str = DEFAULT_ANCHOR_BRANCH,
    clone_dir: str,
) -> dict:
    """Ancla session_id: descarga su JSON completo, computa el registro
    (root + hojas), y hace commit+push a `repo` (clonado en clone_dir --
    el llamador provee un directorio para no acoplar este módulo a una
    ubicación fija ni a gh/git ya clonados de antemano). Requiere `gh` y
    `git` en PATH, con `gh auth status` ya autenticado con scope `repo`
    (nunca credenciales embebidas en este módulo)."""

    session_id = _require_session_id(session_id)
    session_doc = fetch_session_document(session_id, base_url=base_url)
    record = build_anchor_record(session_doc)

    clone_result = subprocess.run(
        ["gh", "repo", "clone", repo, clone_dir, "--", "--branch", branch, "--depth", "1"],
        capture_output=True, text=True, timeout=60,
    )
    if clone_result.returncode != 0:
        raise AnchorError(f"gh repo clone failed: {clone_result.stderr.strip()}")

    anchors_dir = f"{clone_dir}/anchors"
    subprocess.run(["mkdir", "-p", anchors_dir], check=True, timeout=10)
    anchor_path = f"{anchors_dir}/{session_id}.json"
    with open(anchor_path, "w", encoding="utf-8") as handle:
        json.dump(record, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    # Identidad local (scoped a clone_dir, NUNCA --global) -- no depender
    # de que la máquina del operador ya tenga user.name/user.email
    # configurados globalmente (encontrado como fallo real al probar contra
    # un repo desechable, ver PART 3 validación local).
    _run_git(["config", "user.name", "NEXUS Red-Team Anchor"], cwd=clone_dir)
    _run_git(["config", "user.email", "noreply@nexus-redteam.local"], cwd=clone_dir)

    _run_git(["add", f"anchors/{session_id}.json"], cwd=clone_dir)
    _run_git(
        [
            "commit", "-m",
            f"anchor: red-team session {session_id} (merkle_root={record['merkle_root'][:16]}...)",
        ],
        cwd=clone_dir,
    )
    _run_git(["push", "origin", branch], cwd=clone_dir)
    commit_sha = _run_git(["rev-parse", "HEAD"], cwd=clone_dir)

    return {
        "session_id": session_id,
        "repo": repo,
        "commit_sha": commit_sha,
        "anchor_url": f"https://raw.githubusercontent.com/{repo}/{commit_sha}/anchors/{session_id}.json",
        "merkle_root": record["merkle_root"],
        "incident_count": record["incident_count"],
    }


def fetch_anchor_record(
    session_id: str, *, repo: str = DEFAULT_ANCHOR_REPO, branch: str = DEFAULT_ANCHOR_BRANCH
) -> dict:
    """Lee el registro anclado desde raw.githubusercontent.com -- HTTP
    público plano, sin `gh`, sin token: exactamente lo que un juez puede
    ejecutar de forma independiente."""

    session_id = _require_session_id(session_id)
    url = f"https://raw.githubusercontent.com/{repo}/{branch}/anchors/{session_id}.json"
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        raise AnchorError(f"failed to fetch anchor record from {url}: {exc}") from exc


def verify_session(
    session_id: str,
    *,
    base_url: str = DEFAULT_CLOUD_RUN_BASE_URL,
    repo: str = DEFAULT_ANCHOR_REPO,
    branch: str = DEFAULT_ANCHOR_BRANCH,
) -> dict:
    """MATCH | TAMPER_DETECTED, ejecutable por un juez con cero
    credenciales (ver docstring del módulo). Dos capas independientes,
    ambas reusando red_team_incident/red_team_merkle sin modificarlos:

    1. Cadena interna (verify_session_chain, ya existente, sin cambios):
       ¿cada incident_hash sigue siendo el que build_incident() calcularía
       para esos mismos campos, y el encadenado previous_incident_hash es
       intacto? Detecta cualquier edición de UN campo sin recomputar
       también su hash -- el caso "modificar 1 byte" más simple y común.
    2. Anchor externo (esta sesión): ¿merkle_root(hojas actuales) ==
       merkle_root anclado en GitHub? Detecta además el caso más
       sofisticado en que un atacante reescribe campo Y hash de forma
       autoconsistente (pasaría la capa 1) -- solo el commit ya publicado
       fuera del dominio de NEXUS puede exponer eso. Localiza la hoja
       exacta comparando contra leaf_hashes ya publicado en el anchor
       (ver build_anchor_record)."""

    session_id = _require_session_id(session_id)
    current_doc = fetch_session_document(session_id, base_url=base_url)
    anchored = fetch_anchor_record(session_id, repo=repo, branch=branch)

    incidents = tuple(
        red_team_incident.build_incident(
            session_id=item["session_id"], attack_round=item["attack_round"],
            raw_attack_payload=item["raw_attack_payload"], rejection_reason=item["rejection_reason"],
            blocked=item["blocked"], timestamp=item["timestamp"],
            previous_incident_hash=item["previous_incident_hash"], incident_id=item["incident_id"],
        )
        for item in current_doc["incidents"]
    )
    session = red_team_incident.build_session(
        session_id=current_doc["session_id"], started_at=current_doc["started_at"],
        goal=current_doc["goal"], incidents=incidents,
    )
    chain_intact = red_team_incident.verify_session_chain(session)

    current_leaves = _leaf_hashes(current_doc)
    match, leaf_index = red_team_merkle.verify_merkle(
        current_leaves, anchored["merkle_root"], expected_leaf_hashes=anchored.get("leaf_hashes")
    )

    if match and chain_intact:
        return {"status": "MATCH", "session_id": session_id, "merkle_root": anchored["merkle_root"]}

    # Localización por campo (independiente del anchor externo): incidents
    # ya se reconstruyó arriba vía build_incident() a partir de los OTROS
    # campos de current_doc (nunca del incident_hash guardado) -- su
    # .incident_hash es, por construcción, el hash que ESOS campos
    # producirían hoy. Si un atacante edita un campo sin recomputar
    # también el incident_hash guardado, la capa Merkle (arriba) no ve
    # nada distinto -- los hashes guardados no cambiaron -- pero esta
    # comparación sí: es el mismo caso "modificar 1 byte" más simple y
    # común, cubierto sin depender del anchor externo en absoluto.
    recomputed_leaves = [incident.incident_hash for incident in incidents]
    field_tamper_index = next(
        (i for i, (stored, fresh) in enumerate(zip(current_leaves, recomputed_leaves)) if stored != fresh),
        None,
    )

    result = {
        "status": "TAMPER_DETECTED",
        "session_id": session_id,
        "chain_intact": chain_intact,
        "merkle_match": match,
        "anchored_root": anchored["merkle_root"],
        "computed_root": red_team_merkle.merkle_root(current_leaves),
    }
    if leaf_index is not None:
        # Localizado contra el anchor externo -- cubre además el caso más
        # sofisticado en que campo Y hash fueron reescritos de forma
        # autoconsistente (pasaría la comparación de arriba).
        result["leaf_index"] = leaf_index
        result["expected_hash"] = anchored["leaf_hashes"][leaf_index]
        result["actual_hash"] = current_leaves[leaf_index]
        if leaf_index < len(incidents):
            result["incident_id"] = incidents[leaf_index].incident_id
    elif field_tamper_index is not None:
        result["leaf_index"] = field_tamper_index
        result["incident_id"] = incidents[field_tamper_index].incident_id
        result["expected_hash"] = recomputed_leaves[field_tamper_index]
        result["actual_hash"] = current_leaves[field_tamper_index]
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="External Proof Anchor -- red-team session anchor/verify")
    sub = parser.add_subparsers(dest="command", required=True)

    anchor_parser = sub.add_parser("anchor", help="anchor an escalated session (operator-only, needs gh auth)")
    anchor_parser.add_argument("session_id")
    anchor_parser.add_argument("--clone-dir", required=True)
    anchor_parser.add_argument("--base-url", default=DEFAULT_CLOUD_RUN_BASE_URL)
    anchor_parser.add_argument("--repo", default=DEFAULT_ANCHOR_REPO)
    anchor_parser.add_argument("--branch", default=DEFAULT_ANCHOR_BRANCH)

    verify_parser = sub.add_parser("verify", help="judge-runnable, zero credentials required")
    verify_parser.add_argument("session_id")
    verify_parser.add_argument("--base-url", default=DEFAULT_CLOUD_RUN_BASE_URL)
    verify_parser.add_argument("--repo", default=DEFAULT_ANCHOR_REPO)
    verify_parser.add_argument("--branch", default=DEFAULT_ANCHOR_BRANCH)

    args = parser.parse_args(argv)
    try:
        if args.command == "anchor":
            result = anchor_session(
                args.session_id, base_url=args.base_url, repo=args.repo,
                branch=args.branch, clone_dir=args.clone_dir,
            )
        else:
            result = verify_session(
                args.session_id, base_url=args.base_url, repo=args.repo, branch=args.branch
            )
    except AnchorError as exc:
        print(json.dumps({"status": "FAILED", "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = (
    "AnchorError",
    "fetch_session_document",
    "build_anchor_record",
    "anchor_session",
    "fetch_anchor_record",
    "verify_session",
    "main",
)
