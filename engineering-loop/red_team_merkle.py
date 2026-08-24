"""Árbol de Merkle binario sobre incident_hash de una sesión red-team (M-9
follow-up: External Proof Anchor). Combina hashes YA producidos por
red_team_incident.build_incident -- este módulo nunca hashea el contenido
de un incidente, solo combina los incident_hash existentes en un único
root compacto, apto para anclar fuera del dominio de confianza de NEXUS
(ver PART 3, anchor a GitHub público).

Construcción estándar (nivel por nivel, duplicando el último nodo cuando
la cuenta es impar -- mismo patrón que Bitcoin/Certificate Transparency
usan para este caso). Los nodos internos se combinan con un dominio
separado (_MERKLE_NODE_DOMAIN) del dominio que ya usa
_compute_incident_hash en red_team_incident.py, para que un hash de hoja
nunca pueda reinterpretarse como un hash de nodo interno o viceversa
(mitigación estándar del ataque de segunda preimagen entre hoja/nodo en
árboles de Merkle)."""

from __future__ import annotations

import hashlib
from typing import Optional


_MERKLE_NODE_DOMAIN = b"NEXUS-REDTEAM-MERKLE-NODE-V1\x00"


def _combine(left: str, right: str) -> str:
    return hashlib.sha256(
        _MERKLE_NODE_DOMAIN + bytes.fromhex(left) + bytes.fromhex(right)
    ).hexdigest()


def merkle_root(leaf_hashes: list[str]) -> str:
    """Root de Merkle sobre `leaf_hashes` (típicamente
    [incident.incident_hash for incident in session.incidents], en orden).
    Una sola hoja -> esa hoja ES el root (convención estándar). Lista
    vacía -> ValueError (una sesión sin incidentes no tiene nada que
    anclar; nunca se inventa un root para un conjunto vacío)."""

    if not leaf_hashes:
        raise ValueError("leaf_hashes must be non-empty")

    level = list(leaf_hashes)
    while len(level) > 1:
        if len(level) % 2 == 1:
            level.append(level[-1])
        level = [_combine(level[i], level[i + 1]) for i in range(0, len(level), 2)]
    return level[0]


def verify_merkle(
    leaf_hashes: list[str],
    anchored_root: str,
    expected_leaf_hashes: Optional[list[str]] = None,
) -> tuple[bool, Optional[int]]:
    """(True, None) si merkle_root(leaf_hashes) == anchored_root -- MATCH.

    Si no coincide, TAMPER_DETECTED: (False, leaf_index) cuando
    expected_leaf_hashes se provee (la lista de referencia contra la que
    comparar posición por posición -- en el flujo real de verify_session,
    esto es el incident_hash RECOMPUTADO desde los otros campos guardados
    de cada incidente, ver red_team_incident._compute_incident_hash; si
    difiere del incident_hash ALMACENADO en esa misma posición, esa es la
    hoja alterada). Sin expected_leaf_hashes: (False, None) -- un mismatch
    de root por sí solo, sin nada contra qué comparar posición por
    posición, NO puede localizar matemáticamente qué hoja cambió (un
    único hash de 256 bits no es invertible ni codifica la posición de
    una diferencia dentro de N hojas); afirmar un índice sin esa
    referencia sería fabricar una certeza que el cómputo no respalda."""

    if merkle_root(leaf_hashes) == anchored_root:
        return True, None
    if expected_leaf_hashes is None:
        return False, None
    for index, (actual, expected) in enumerate(zip(leaf_hashes, expected_leaf_hashes)):
        if actual != expected:
            return False, index
    # Mismo prefijo común, pero largos distintos (hoja añadida/eliminada):
    # la primera posición donde una lista ya no tiene contraparte.
    return False, min(len(leaf_hashes), len(expected_leaf_hashes))


__all__ = ("merkle_root", "verify_merkle")
