"""Supervision de muerte del proceso padre para el hijo aislado real
(M-AG012, endurecido en M-AG014). Cierra el hallazgo #1 de la auditoria
independiente M-AG011: procesos hijo reales quedaron huerfanos y vivos
porque su proceso padre de Python fue terminado externamente antes de
completar su propia secuencia de matado. Sin ningun mecanismo de
supervision del lado del hijo, un proceso huerfano puede sobrevivir
indefinidamente si nada mas en el sistema vuelve a intentar matarlo.

CORRECCION M-AG014 (cierra el hallazgo de la auditoria independiente
M-AG013, reproducido 25/25 veces contra la version anterior de este
modulo): la version M-AG012 de `start_parent_watchdog()` capturaba su
PID de referencia "original" de forma PEREZOSA -- `original_ppid =
ppid_probe()`, la primera vez que el hilo watchdog corria. Si el padre
ya habia muerto ANTES de que esa linea llegara a ejecutarse (tipico:
import de modulos, arranque del interprete, decenas de milisegundos),
el kernel ya habia re-parentado a este proceso a init/systemd en ese
momento, y el watchdog capturaba ese valor YA CONTAMINADO como su
"linea base". Como nunca vuelve a cambiar, el watchdog comparaba el
mismo numero contra si mismo para siempre y nunca disparaba -- el
mismo gotcha documentado de PR_SET_PDEATHSIG (no se entrega si el
padre ya murio antes de la llamada a prctl(2)) reproducido en la logica
de respaldo que se suponia debia cubrir exactamente ese caso.

La correccion: el PADRE calcula su propio PID (`os.getpid()`) ANTES de
spawnear al hijo y se lo pasa como informacion de arranque INMUTABLE
(`--expected-parent-pid <PID>`, ver antigravity_isolated_child.py). El
hijo nunca descubre su "linea base" por su cuenta -- la recibe ya
conocida, y la usa para 3 verificaciones independientes en capas:

Capa 1 -- verificacion inicial: `verify_parent_or_die()` compara
os.getppid() contra expected_parent_pid ANTES de instalar PDEATHSIG.
Si el padre ya murio en la ventana entre el spawn y este punto, se
detecta aqui, en la primera instruccion ejecutable posible.

Capa 2 -- PR_SET_PDEATHSIG (mecanismo primario, solo Linux): arma un
flag en el kernel para que, cuando el proceso padre termine por
cualquier motivo -- salida normal, SIGTERM, SIGKILL, crash -- el kernel
entregue una senal a este proceso automaticamente, incluso si este
proceso esta bloqueado en I/O (p.ej. esperando la respuesta de un
futuro backend real de google.genai).

Capa 3 -- segunda verificacion post-instalacion: `verify_parent_or_die()`
se llama otra vez inmediatamente despues de install_pdeathsig(), para
cerrar la ventana de carrera especifica entre "el padre podria haber
muerto justo durante la instalacion" y "PDEATHSIG ya esta armado".

Capa 4 -- watchdog de sondeo (respaldo de monitoreo sostenido): un hilo
daemon que compara periodicamente el PID del padre inmediato contra
expected_parent_pid (el MISMO valor conocido de antemano, nunca una
linea base descubierta por el propio watchdog). Cubre la ventana larga
posterior -- el padre muere minutos despues, mientras este proceso esta
ocupado en una llamada de red real -- y cualquier plataforma/entorno
restringido donde prctl(2) no este disponible.

Todas las capas viven y mueren con el mismo proceso hijo desechable que
las instala -- ningun registro global, ningun estado compartido entre
invocaciones, ningun daemon de larga duracion independiente. Esto
preserva el invariante N.1 ya establecido por
antigravity_isolated_transport.py: una peticion, un proceso hijo, un
ciclo de vida propio.

ANALISIS DE REUSO DE PID (Fase 3 del brief M-AG014): la igualdad de PID
NO es identidad criptografica de proceso -- en teoria, si el PID
original del padre fuera reasignado por el kernel a otro proceso no
relacionado durante la ventana de spawn, una comparacion de PID podria
en principio dar un falso positivo. En la practica, esto no es
explotable en este boundary: cuando el padre muere, este proceso es
re-parentado por el kernel a init/systemd/un subreaper de contenedor --
nunca a un proceso arbitrario que casualmente reutilice el PID viejo del
padre, porque el kernel no re-parenta hacia PIDs reciclados, re-parenta
hacia el subreaper mas cercano en el arbol de procesos. Para que el
reuso de PID produjera un falso "padre sigue vivo", se necesitaria que
el NUEVO proceso con el PID reciclado se convirtiera literalmente en el
padre real de este hijo (os.getppid() lo reportaria), lo cual requiere
que ese nuevo proceso haga ptrace/reparenting explicito hacia este PID
especifico -- un ataque deliberado y activo, no un accidente de
scheduling del PID allocator, y muy por fuera del modelo de amenaza de
"padre normal que muere/crashea". No se introduce ningun mecanismo
adicional (p.ej. verificar tambien el tiempo de arranque del proceso
padre via /proc/<pid>/stat) porque el riesgo residual es teorico y la
complejidad adicional no esta justificada por el brief ("no introducir
arquitectura mayor solo para resolver un escenario de reuso de PID
teoricamente insignificante").
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import signal
import sys
import threading
import time
from typing import Callable, Optional

# Ver linux/prctl.h -- valor estable de la ABI de Linux, no requiere
# ningun paquete adicional para conocerlo.
_PR_SET_PDEATHSIG = 1

_DEFAULT_POLL_INTERVAL_SECONDS = 0.2


def install_pdeathsig(sig: "signal.Signals" = signal.SIGTERM) -> bool:
    """Intenta armar PR_SET_PDEATHSIG via prctl(2). Devuelve True si el
    kernel confirmo la llamada, False en cualquier otro caso -- nunca
    lanza. Solo disponible en Linux; en cualquier otra plataforma, o si
    el syscall esta bloqueado (contenedor restringido, seccomp, etc.),
    el llamador debe confiar exclusivamente en el watchdog de respaldo
    (Objetivo B)."""

    if not sys.platform.startswith("linux"):
        return False

    try:
        libc_name = ctypes.util.find_library("c") or "libc.so.6"
        libc = ctypes.CDLL(libc_name, use_errno=True)
    except OSError:
        return False

    try:
        result = libc.prctl(_PR_SET_PDEATHSIG, int(sig), 0, 0, 0)
    except (OSError, AttributeError, TypeError):
        return False

    return result == 0


def verify_parent_or_die(
    expected_parent_pid: int,
    *,
    ppid_probe: Callable[[], int] = os.getppid,
    on_mismatch: Optional[Callable[[], None]] = None,
) -> None:
    """Capa 1/3 (M-AG014): compara el PID del padre inmediato AHORA MISMO
    contra `expected_parent_pid` -- un valor conocido de antemano por el
    padre, nunca una linea base descubierta perezosamente por este mismo
    proceso. Fail-closed: si no coinciden, termina el proceso de
    inmediato via os._exit(1) por defecto (nunca continua con ejecucion
    no supervisada). `on_mismatch` es inyectable solo para pruebas
    deterministas -- el comportamiento de produccion real (os._exit) se
    prueba por separado con arboles de procesos reales."""

    def _terminate_self() -> None:
        os._exit(1)

    _on_mismatch = on_mismatch or _terminate_self

    if ppid_probe() != expected_parent_pid:
        _on_mismatch()


def start_parent_watchdog(
    expected_ppid: int,
    *,
    poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL_SECONDS,
    on_orphaned: Optional[Callable[[], None]] = None,
    ppid_probe: Callable[[], int] = os.getppid,
) -> threading.Thread:
    """Arranca el hilo daemon de respaldo (Capa 4). CORRECCION M-AG014:
    `expected_ppid` es ahora un parametro OBLIGATORIO, el mismo valor
    conocido de antemano que usa verify_parent_or_die() -- este hilo ya
    NO captura su propia linea base de forma perezosa (esa era la causa
    raiz del hallazgo de M-AG013: si el padre ya habia muerto antes de
    que esta funcion llegara a ejecutarse, la linea base perezosa
    quedaba contaminada y el watchdog nunca disparaba). Anclar contra un
    valor fijo conocido de antemano hace que el watchdog sea correcto en
    cualquier momento de su ciclo de vida, no solo despues de su propio
    arranque.

    `ppid_probe` sigue siendo inyectable para poder probar la logica de
    deteccion de forma determinista y rapida en tests unitarios, sin
    depender de terminar un proceso real del sistema operativo -- el
    comportamiento de punta a punta contra un proceso real se prueba por
    separado con un arbol de procesos de verdad.

    El callback por defecto usa os._exit(1): una salida dura que ningun
    manejador de senales instalado accidentalmente puede interceptar,
    sin ejecutar limpieza de Python que pudiera colgarse -- coherente
    con el resto de este boundary, donde un hijo comprometido/colgado
    nunca debe poder impedir su propia terminacion."""

    def _terminate_self() -> None:
        os._exit(1)

    _on_orphaned = on_orphaned or _terminate_self

    def _watch() -> None:
        while True:
            time.sleep(poll_interval_seconds)
            if ppid_probe() != expected_ppid:
                _on_orphaned()
                return

    thread = threading.Thread(target=_watch, name="parent-death-watchdog", daemon=True)
    thread.start()
    return thread


def supervise_parent_death(
    expected_parent_pid: int,
    *,
    sig: "signal.Signals" = signal.SIGTERM,
    poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL_SECONDS,
    ppid_probe: Callable[[], int] = os.getppid,
) -> None:
    """Punto de entrada unico que un proceso hijo aislado real debe
    llamar como su PRIMERA accion posible -- antes de leer stdin, antes
    de invocar cualquier backend de larga duracion, antes de cualquier
    inicializacion costosa. Invariante futuro: NO VERIFIED LIVE PARENT =
    NO GEMINI CALL.

    `expected_parent_pid` lo calcula el PADRE (os.getpid(), ANTES de
    spawnear) y se lo pasa a este proceso como informacion de arranque
    inmutable -- ver antigravity_isolated_child.py::_extract_expected_
    parent_pid(). Ejecuta las 4 capas de defensa EN ORDEN: verificacion
    inicial -> PDEATHSIG -> segunda verificacion -> watchdog de
    respaldo. install_pdeathsig() puede fallar silenciosamente
    (plataforma sin soporte) sin que eso impida que las demas capas
    queden activas de todas formas."""

    verify_parent_or_die(expected_parent_pid, ppid_probe=ppid_probe)
    install_pdeathsig(sig)
    verify_parent_or_die(expected_parent_pid, ppid_probe=ppid_probe)
    start_parent_watchdog(expected_parent_pid, poll_interval_seconds=poll_interval_seconds, ppid_probe=ppid_probe)


__all__ = (
    "install_pdeathsig",
    "verify_parent_or_die",
    "start_parent_watchdog",
    "supervise_parent_death",
)
