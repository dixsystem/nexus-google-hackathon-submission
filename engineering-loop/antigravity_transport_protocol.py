"""Marcador minimo y neutral (M-AG010) para distinguir, en tiempo de
ejecucion, un transport que soporta cancel_event de punta a punta en su
metodo run() de un callable simple de 4 argumentos posicionales (el
protocolo Transport original de M-AG006: model_id, prompt, format,
timeout_seconds).

Existe en su propio modulo, sin depender de ni ser importado por
antigravity_gemini_provider.py ni antigravity_isolated_transport.py de
forma ciclica: antigravity_gemini_provider.py lo importa para el chequeo
isinstance(), antigravity_isolated_transport.py lo importa para que
IsolatedGeminiTransport herede de el. antigravity_isolated_transport.py
ya importa RawGeminiResult de antigravity_gemini_provider.py, asi que si
este marcador viviera en cualquiera de esos dos modulos se crearia un
import circular.

El chequeo debe ser isinstance() real (herencia/registro efectivo de una
ABC), nunca duck-typing por hasattr()/getattr(): unittest.mock.Mock()
autogenera cualquier atributo que se le consulte (incluyendo .run), asi
que hasattr(mock.Mock(), "run") es True aunque nadie lo haya configurado
-- eso produciria un falso positivo y rompería silenciosamente los 39
tests existentes de M-AG006/M-AG008 en test_antigravity_gemini_provider.py,
que usan mock.Mock() como transport y aseveran la firma exacta de 4
argumentos posicionales con la que fue invocado. isinstance() contra una
ABC real solo es verdadero para subclases reales o registradas
explicitamente -- nunca para un Mock() sin configurar, que no es
subclase de esta clase ni fue registrado con ella.
"""

from __future__ import annotations

import abc


class CancellableTransport(abc.ABC):
    """Un transport que hereda de esta clase expone, ademas del protocolo
    Transport original de 4 argumentos posicionales via __call__, un
    metodo run(model_id, prompt, format, timeout_seconds, *, cancel_event)
    que propaga una cancelacion iniciada por el llamador hasta su propia
    implementacion de terminacion (p.ej. matar un proceso hijo real)."""

    @abc.abstractmethod
    def run(self, model_id: str, prompt: str, format, timeout_seconds: float, *, cancel_event=None):
        ...


__all__ = ("CancellableTransport",)
