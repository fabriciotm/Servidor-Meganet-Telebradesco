from __future__ import annotations

import ast
import pathlib
import subprocess
import sys
import symtable

import meganet_server
from meganet_server import api, server
from meganet_server._version import __version__ as canonical_version
from meganet_server.admin.auth import format_datetime as admin_format_datetime
from meganet_server.common import format_datetime as common_format_datetime


ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "meganet_server"


def _imports(path: pathlib.Path) -> list[tuple[str, int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((alias.name, 0) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.append((node.module or "", node.level))
    return found


def test_version_has_one_runtime_source_of_truth() -> None:
    assert canonical_version == "1.0.0"
    assert meganet_server.__version__ == canonical_version
    assert api.__version__ == canonical_version
    assert server.__version__ == canonical_version


def test_runtime_and_protocol_layers_do_not_depend_on_admin_or_legacy_server() -> None:
    violations: list[str] = []
    for area in ("runtime", "meganet", "telebradesco"):
        for path in (PACKAGE / area).rglob("*.py"):
            for module, level in _imports(path):
                # Relative ``..admin`` appears as module="admin...", level=2.
                if (level >= 2 and (module == "admin" or module.startswith("admin."))) or module.startswith("meganet_server.admin"):
                    violations.append(f"{path.relative_to(ROOT)} -> admin")
                if (level >= 2 and (module == "server" or module.startswith("server."))) or module == "meganet_server.server":
                    violations.append(f"{path.relative_to(ROOT)} -> server")
    assert violations == []


def test_cli_no_longer_routes_through_legacy_server() -> None:
    cli_source = (PACKAGE / "cli.py").read_text(encoding="utf-8")
    assert "from .server import" not in cli_source
    assert "from .admin.server import start_admin_server" in cli_source


def test_runtime_datetime_formatter_is_layer_neutral_and_reexported_for_admin() -> None:
    assert admin_format_datetime is common_format_datetime
    assert common_format_datetime("2026-10-02T21:30:00") == "02/10/2026 21:30"


def test_legacy_server_remains_a_small_facade() -> None:
    source = (PACKAGE / "server.py").read_text(encoding="utf-8")
    assert len(source.splitlines()) < 60
    tree = ast.parse(source)
    assert not any(isinstance(node, (ast.ClassDef,)) for node in tree.body)


def test_package_module_entrypoint_supports_help() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "meganet_server", "--help"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "usage:" in proc.stdout.lower()


def test_generic_exception_barriers_are_bounded() -> None:
    # Broad catches remain allowed at session/listener/controller containment
    # boundaries.  The threshold prevents them from silently proliferating in
    # parsing and persistence code again.
    total = 0
    for path in PACKAGE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler) or node.type is None:
                continue
            if isinstance(node.type, ast.Name) and node.type.id == "Exception":
                total += 1
    assert total <= 45


def test_modules_do_not_reference_missing_global_symbols() -> None:
    builtins_names = set(dir(__builtins__)) if not isinstance(__builtins__, dict) else set(__builtins__)
    allowed = {("meganet/content.py", "item")}
    violations: list[str] = []
    for path in PACKAGE.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        module_defs: set[str] = set()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                module_defs.add(node.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    module_defs.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    module_defs.add(alias.asname or alias.name)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        module_defs.add(target.id)
        table = symtable.symtable(source, str(path), "exec")
        referenced_globals: set[str] = set()
        def walk(scope) -> None:
            for ident in scope.get_identifiers():
                symbol = scope.lookup(ident)
                if symbol.is_global() and symbol.is_referenced():
                    referenced_globals.add(ident)
            for child in scope.get_children():
                walk(child)
        walk(table)
        relative = path.relative_to(PACKAGE).as_posix()
        for name in sorted(referenced_globals - module_defs - builtins_names - {"__name__", "__file__", "__package__"}):
            if (relative, name) not in allowed:
                violations.append(f"{relative}: {name}")
    assert violations == []


def test_meganet_session_has_registration_email_fallback_imported() -> None:
    from meganet_server.meganet import accounts, session
    assert session.meganet_email_for_name is accounts.meganet_email_for_name


def test_telebradesco_legacy_form_adapter_is_self_contained() -> None:
    from meganet_server.telebradesco.accounts import telebradesco_account_from_form
    from meganet_server.telebradesco.content import blank_telebradesco_content

    account = telebradesco_account_from_form({
        "tele_account_name": ["Teste"],
        "tele_agencia": ["2924"],
        "tele_conta": ["0023456"],
        "tele_digito": ["7"],
        "tele_senha": ["1234"],
    }, blank_telebradesco_content())
    assert account["nome"] == "Teste"
    assert account["agencia"] == "2924"
    assert account["conta"] == "0023456"
    assert account["digito"] == "7"


def test_all_package_defined_type_hints_resolve() -> None:
    """Postponed annotations must still be resolvable by tooling at runtime."""
    import importlib
    import inspect
    import pkgutil
    import typing

    failures: list[str] = []
    for info in pkgutil.walk_packages(meganet_server.__path__, meganet_server.__name__ + "."):
        module = importlib.import_module(info.name)
        for name, obj in vars(module).items():
            if getattr(obj, "__module__", None) != module.__name__:
                continue
            if not (inspect.isfunction(obj) or inspect.isclass(obj)):
                continue
            if not getattr(obj, "__annotations__", None):
                continue
            try:
                typing.get_type_hints(obj)
            except Exception as exc:  # failure is reported with exact symbol below
                failures.append(f"{module.__name__}.{name}: {type(exc).__name__}: {exc}")
    assert failures == []


def test_codebase_audit_script_passes() -> None:
    proc = subprocess.run(
        [sys.executable, "tools/audit_codebase.py"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "resultado: OK" in proc.stdout
