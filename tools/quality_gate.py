#!/usr/bin/env python3
"""Quality gate local do Servidor MegaNet / Telebradesco."""
from __future__ import annotations

import compileall
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def run(*args: str) -> None:
    print("+", " ".join(args), flush=True)
    subprocess.run(args, cwd=ROOT, check=True)


def main() -> int:
    if not compileall.compile_dir(ROOT / "meganet_server", quiet=1):
        print("ERROR: compileall falhou", file=sys.stderr)
        return 1
    run(sys.executable, "tools/audit_codebase.py")
    run(sys.executable, "-m", "meganet_server", "--help")
    run(sys.executable, "servidor_meganet_telebradesco.py", "--help")
    run(
        sys.executable, "-m", "pytest",
        "--cov=meganet_server",
        "--cov-report=term",
        "--cov-fail-under=60",
    )
    print("quality gate: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
