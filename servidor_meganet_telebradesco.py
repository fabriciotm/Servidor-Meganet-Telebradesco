#!/usr/bin/env python3
"""Launcher for the modular MegaNet / Telebradesco server package."""
from __future__ import annotations

import sys
from meganet_server.api import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
