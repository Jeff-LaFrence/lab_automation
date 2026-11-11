"""Registry of instrument backends, keyed by the workcell's "instrument" string."""

from __future__ import annotations

from labauto.instruments.base import Instrument


def _ot2() -> Instrument:
    from labauto.instruments.opentrons_ot2 import OpentronsOT2

    return OpentronsOT2()


def _tecan() -> Instrument:
    from labauto.instruments.tecan_evo import TecanEVO

    return TecanEVO()


REGISTRY = {
    "opentrons_ot2": _ot2,
    "tecan_evo": _tecan,
}


def get_instrument(name: str) -> Instrument:
    """Return the backend for an instrument name. Raises KeyError for an unknown name."""
    if name not in REGISTRY:
        raise KeyError(name)
    return REGISTRY[name]()


__all__ = ["Instrument", "REGISTRY", "get_instrument"]
