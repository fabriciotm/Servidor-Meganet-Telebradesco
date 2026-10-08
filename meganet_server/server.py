#!/usr/bin/env python3
"""Legacy compatibility facade for the MegaNet / Telebradesco server.

Operational code lives in the domain, runtime, configuration and admin modules.
New integrations should import from :mod:`meganet_server.api`.
"""

from __future__ import annotations

import argparse
import sys

from ._version import __version__
from .admin.server import start_admin_server
from .cli import main
from .compat import legacy_server_dir, resolve_legacy_server_export
from .runtime.orchestration import run_services



def __getattr__(name: str) -> object:
    """Resolve historical ``server.<name>`` exports lazily."""
    value = resolve_legacy_server_export(name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(legacy_server_dir()))


async def amain(args: argparse.Namespace) -> None:
    """Compatibility wrapper around the modular runtime orchestrator."""
    await run_services(args, admin_server_factory=start_admin_server)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
