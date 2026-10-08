#!/usr/bin/env python3
"""Auditoria estática/arquitetural da base MegaNet/Telebradesco.

Não depende de ferramentas externas. Deve ser executado a partir da raiz do
projeto com::

    python tools/audit_codebase.py

Retorna código diferente de zero quando uma invariável arquitetural importante
é violada. Métricas informativas também são exibidas para facilitar revisões.
"""
from __future__ import annotations

import ast
import importlib
import inspect
import pathlib
import pkgutil
import sys
import typing

ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "meganet_server"

# Imports mantidos deliberadamente como reexports de compatibilidade local.
# Qualquer outro import aparentemente não usado é tratado como regressão.
INTENTIONAL_REEXPORTS = {
    ("__init__.py", "__version__"),
    ("server.py", "__version__"),
    ("admin/auth.py", "format_datetime"),
    ("admin/server.py", "_request_close_meganet_sessions"),
    ("meganet/session.py", "STX"),
    ("meganet/session.py", "ETX"),
    ("meganet/session.py", "encode_server_frame"),
    ("meganet/session.py", "DEFAULT_CADASTRO_DB"),
    ("meganet/session.py", "DEFAULT_MESSAGE"),
    ("meganet/session.py", "CONTENT_BLANK_LINE_MARKER"),
}


def _imports(path: pathlib.Path) -> list[tuple[str, int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((alias.name, 0) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.append((node.module or "", node.level))
    return found


def _unused_imports(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    used = {
        node.id for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }
    exported: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets):
            continue
        if isinstance(node.value, (ast.List, ast.Tuple)):
            exported.update(
                element.value for element in node.value.elts
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            )
    imports: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "__future__":
                    continue
                imports.append(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module == "__future__":
                continue
            for alias in node.names:
                if alias.name != "*":
                    imports.append(alias.asname or alias.name)
    return sorted(name for name in imports if name not in used and name not in exported)


def main() -> int:
    sys.path.insert(0, str(ROOT))
    failures: list[str] = []

    # 1. Camadas de domínio/runtime não podem depender da UI/fachada legada.
    for area in ("runtime", "meganet", "telebradesco"):
        for path in (PACKAGE / area).rglob("*.py"):
            for module, level in _imports(path):
                if (level >= 2 and (module == "admin" or module.startswith("admin."))) or module.startswith("meganet_server.admin"):
                    failures.append(f"dependência proibida: {path.relative_to(ROOT)} -> admin")
                if (level >= 2 and (module == "server" or module.startswith("server."))) or module == "meganet_server.server":
                    failures.append(f"dependência proibida: {path.relative_to(ROOT)} -> server")

    # 2. Fachadas/composição devem permanecer pequenas.
    budgets = {
        PACKAGE / "server.py": 60,
        PACKAGE / "admin" / "server.py": 300,
    }
    for path, limit in budgets.items():
        lines = len(path.read_text(encoding="utf-8").splitlines())
        if lines > limit:
            failures.append(f"orçamento excedido: {path.relative_to(ROOT)} = {lines} linhas > {limit}")

    # 3. Barras defensivas genéricas são permitidas, mas não devem proliferar.
    broad_exceptions = 0
    for path in PACKAGE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and isinstance(node.type, ast.Name) and node.type.id == "Exception":
                broad_exceptions += 1
    if broad_exceptions > 45:
        failures.append(f"except Exception: {broad_exceptions} > 45")

    # 4. Anotações postergadas precisam ser resolvíveis por ferramentas reais.
    import meganet_server
    type_hint_failures: list[str] = []
    module_count = 0
    for info in pkgutil.walk_packages(meganet_server.__path__, meganet_server.__name__ + "."):
        module = importlib.import_module(info.name)
        module_count += 1
        for name, obj in vars(module).items():
            if getattr(obj, "__module__", None) != module.__name__:
                continue
            if not (inspect.isfunction(obj) or inspect.isclass(obj)):
                continue
            if not getattr(obj, "__annotations__", None):
                continue
            try:
                typing.get_type_hints(obj)
            except Exception as exc:
                type_hint_failures.append(
                    f"{module.__name__}.{name}: {type(exc).__name__}: {exc}"
                )
    failures.extend(f"type hint inválido: {item}" for item in type_hint_failures)

    # 5. Imports mortos só são admitidos quando documentados como reexport.
    dead_imports: list[str] = []
    for path in PACKAGE.rglob("*.py"):
        rel = path.relative_to(PACKAGE).as_posix()
        for name in _unused_imports(path):
            if (rel, name) not in INTENTIONAL_REEXPORTS:
                dead_imports.append(f"{rel}: {name}")
    failures.extend(f"import não usado: {item}" for item in dead_imports)

    # 6. Toda a fachada histórica deve continuar resolvendo.
    from meganet_server.compat import LEGACY_SERVER_EXPORTS, resolve_legacy_server_export
    legacy_failures: list[str] = []
    for name in LEGACY_SERVER_EXPORTS:
        try:
            if resolve_legacy_server_export(name) is None:
                legacy_failures.append(name)
        except Exception as exc:
            legacy_failures.append(f"{name} ({type(exc).__name__}: {exc})")
    failures.extend(f"alias legado inválido: {item}" for item in legacy_failures)

    print("MegaNet/Telebradesco codebase audit")
    print(f"  módulos importados: {module_count}")
    print(f"  aliases legados: {len(LEGACY_SERVER_EXPORTS)}")
    print(f"  except Exception: {broad_exceptions}")
    print(f"  type hints inválidos: {len(type_hint_failures)}")
    print(f"  imports mortos não autorizados: {len(dead_imports)}")
    print(f"  falhas: {len(failures)}")
    if failures:
        for failure in failures:
            print(f"ERROR: {failure}", file=sys.stderr)
        return 1
    print("  resultado: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
