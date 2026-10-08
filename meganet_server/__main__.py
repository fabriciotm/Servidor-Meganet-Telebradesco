"""Allow ``python -m meganet_server`` to run the supported CLI."""
from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
