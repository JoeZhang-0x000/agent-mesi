from __future__ import annotations

from pathlib import Path

_src_package = Path(__file__).resolve().parent.parent / "src" / "mesi_runtime"
if _src_package.exists():
    __path__.append(str(_src_package))  # type: ignore[name-defined]

__version__ = "0.1.0"
