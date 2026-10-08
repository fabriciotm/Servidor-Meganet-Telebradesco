"""MegaNet / Telebradesco server package.

The supported integration surface lives in :mod:`meganet_server.api`.
"""
from __future__ import annotations

from ._version import __version__

# Keep package import light: stable API names are loaded only when requested.
def __getattr__(name: str) -> object:
    if name == "api":
        import importlib
        return importlib.import_module(".api", __name__)
    from .api import __all__ as public_names
    if name in public_names:
        import importlib
        public_api = importlib.import_module(".api", __name__)
        return getattr(public_api, name)
    raise AttributeError(name)


def __dir__() -> list[str]:
    from .api import __all__ as public_names
    return sorted(set(globals()) | set(public_names) | {"api"})
