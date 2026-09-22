"""The NI GPIB-USB driver must stand on its own.

``resistamet_gui.gpib_usb`` may import itself, the standard library, pyusb
(``usb``), ``pyvisa`` and ``pyvisa_py``, and nothing else: no other part of
the application and no other third-party package. The modules are parsed,
not imported, so a module's import-time side effects cannot hide or cause
a failure, and an import inside a function counts as much as one at the top.

One import outside that set is allowed by name: linux-gpib's Python binding,
``gpib``, which ``boards`` tries so that its board numbers follow
linux-gpib's. It must stay inside a ``try`` that catches ``ImportError``.
"""
import ast
import importlib.util
import pathlib
import sys
import sysconfig
from typing import Iterator, List, Optional, Tuple

import pytest

PACKAGE = 'resistamet_gui.gpib_usb'
PACKAGE_DIR = pathlib.Path(__file__).resolve().parent.parent / 'resistamet_gui' / 'gpib_usb'
THIRD_PARTY = ('usb', 'pyvisa', 'pyvisa_py')
#: Optional imports allowed only under ``except ImportError``.
OPTIONAL = ('gpib',)


def _is_stdlib(top: str) -> bool:
    names = getattr(sys, 'stdlib_module_names', None)  # Python 3.10+
    if names is not None:
        return top in names or top in sys.builtin_module_names
    # Python 3.9: where the module would load from, without loading it.
    if top in sys.builtin_module_names:
        return True
    spec = importlib.util.find_spec(top)
    if spec is None or spec.origin is None:
        return False
    if spec.origin in ('built-in', 'frozen'):
        return True
    origin = pathlib.Path(spec.origin).resolve()
    stdlib = pathlib.Path(sysconfig.get_paths()['stdlib']).resolve()
    purelib = pathlib.Path(sysconfig.get_paths()['purelib']).resolve()
    return stdlib in origin.parents and purelib not in origin.parents


def _module_name(path: pathlib.Path) -> str:
    parts = path.relative_to(PACKAGE_DIR).with_suffix('').parts
    if parts[-1] == '__init__':
        parts = parts[:-1]
    return '.'.join((PACKAGE,) + parts)


def _resolve(module: str, is_package: bool, node: ast.ImportFrom) -> List[str]:
    """The absolute names an ``ImportFrom`` reaches."""
    if node.level == 0:
        return [node.module or '']
    package = module.split('.') if is_package else module.split('.')[:-1]
    if node.level - 1 > len(package) - 1:
        return ['<relative import above the top-level package>']
    base = package[:len(package) - (node.level - 1)]
    if node.module:
        return ['.'.join(base + [node.module])]
    # ``from . import x``: each name may be a submodule.
    return ['.'.join(base + [alias.name]) for alias in node.names]


def _under_import_error_guard(parents: List[ast.AST], node: ast.AST) -> bool:
    for parent in reversed(parents):
        if isinstance(parent, ast.Try) and any(node is n or _contains(n, node) for n in parent.body):
            for handler in parent.handlers:
                caught = handler.type
                names = caught.elts if isinstance(caught, ast.Tuple) else [caught]
                if any(isinstance(n, ast.Name) and n.id in ('ImportError', 'ModuleNotFoundError')
                       for n in names):
                    return True
    return False


def _contains(tree: ast.AST, node: ast.AST) -> bool:
    return any(child is node for child in ast.walk(tree))


def imports(source: str, module: str, is_package: bool) -> Iterator[Tuple[int, str, bool]]:
    """(line, absolute name, guarded by ``except ImportError``) for every import in a source."""
    tree = ast.parse(source)
    stack: List[Tuple[ast.AST, List[ast.AST]]] = [(tree, [])]
    while stack:
        node, parents = stack.pop()
        names: Optional[List[str]] = None
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = _resolve(module, is_package, node)
        elif isinstance(node, ast.Call):
            func = node.func
            dynamic = ((isinstance(func, ast.Name) and func.id == '__import__')
                       or (isinstance(func, ast.Attribute) and func.attr == 'import_module'))
            if dynamic:
                first = node.args[0] if node.args else None
                literal = isinstance(first, ast.Constant) and isinstance(first.value, str)
                names = [first.value if literal else '<dynamic import>']
        if names is not None:
            guarded = _under_import_error_guard(parents, node)
            for name in names:
                yield node.lineno, name, guarded
        for child in ast.iter_child_nodes(node):
            stack.append((child, parents + [node]))


def violation(name: str, guarded: bool) -> Optional[str]:
    """Why an import breaks the rule, or None."""
    top = name.split('.')[0]
    if name == PACKAGE or name.startswith(PACKAGE + '.'):
        return None
    if top == 'resistamet_gui':
        return 'imports the application'
    if top in THIRD_PARTY or _is_stdlib(top):
        return None
    if top in OPTIONAL:
        return None if guarded else 'optional import outside an except ImportError'
    return 'imports a package outside the standard library, usb, pyvisa and pyvisa_py'


def _modules() -> List[pathlib.Path]:
    return sorted(PACKAGE_DIR.rglob('*.py'))


def _all_imports() -> List[Tuple[str, int, str, bool]]:
    found = []
    for path in _modules():
        module = _module_name(path)
        for line, name, guarded in imports(path.read_text(encoding='utf-8'), module, path.name == '__init__.py'):
            found.append((str(path.relative_to(PACKAGE_DIR)), line, name, guarded))
    return found


def test_the_package_imports_only_itself_the_standard_library_pyusb_and_pyvisa():
    problems = ['%s:%d %s: %s' % (path, line, name, why)
                for path, line, name, guarded in _all_imports()
                for why in [violation(name, guarded)] if why]
    assert problems == []


def test_the_scan_sees_the_package_s_own_imports():
    # A parse that found nothing would pass the test above for the wrong reason.
    names = {name for _, _, name, _ in _all_imports()}
    assert {'usb.core', 'pyvisa_py.sessions', 'resistamet_gui.gpib_usb.protocol',
            'resistamet_gui.gpib_usb.visa_intfc', 'threading', 'gpib'} <= names
    assert len(_modules()) >= 13


@pytest.mark.parametrize('source, module, is_package, why', [
    ('from .. import constants', PACKAGE + '.boards', False, 'imports the application'),
    ('from ..instrument import Keithley2400', PACKAGE + '.boards', False, 'imports the application'),
    ('from .. import config', PACKAGE, True, 'imports the application'),
    ('import resistamet_gui.constants', PACKAGE + '.boards', False, 'imports the application'),
    ('from resistamet_gui import visa_backend', PACKAGE + '.boards', False, 'imports the application'),
    ('def f():\n    import numpy', PACKAGE + '.boards', False, 'outside the standard library'),
    ('import PySide6.QtCore', PACKAGE + '.boards', False, 'outside the standard library'),
    ('import importlib\nimportlib.import_module("resistamet_gui.config")', PACKAGE + '.boards', False,
     'imports the application'),
    ('import gpib', PACKAGE + '.boards', False, 'optional import outside'),
    ('try:\n    import gpib\nexcept OSError:\n    pass', PACKAGE + '.boards', False, 'optional import outside'),
])
def test_the_check_catches(source, module, is_package, why):
    reasons = [violation(name, guarded) for _, name, guarded in imports(source, module, is_package)]
    assert any(reason and why in reason for reason in reasons), reasons


@pytest.mark.parametrize('source, module, is_package', [
    ('from . import protocol as p', PACKAGE + '.boards', False),
    ('from .transport import TransportError', PACKAGE + '.boards', False),
    ('from . import visa_session', PACKAGE, True),
    ('import resistamet_gui.gpib_usb.protocol', PACKAGE + '.boards', False),
    ('import logging, os, threading\nfrom typing import Optional', PACKAGE + '.boards', False),
    ('import usb.core\nfrom usb.backend import libusb1', PACKAGE + '.transport', False),
    ('from pyvisa import constants\nfrom pyvisa_py.sessions import Session', PACKAGE + '.visa_session', False),
    ('try:\n    import gpib\nexcept ImportError:\n    pass', PACKAGE + '.boards', False),
])
def test_the_check_allows(source, module, is_package):
    reasons = [violation(name, guarded) for _, name, guarded in imports(source, module, is_package)]
    assert reasons and all(reason is None for reason in reasons), reasons


def test_the_python_3_9_standard_library_check_agrees(monkeypatch):
    # sys.stdlib_module_names is new in 3.10; the suite also runs on 3.9.
    tops = sorted({name.split('.')[0] for _, _, name, _ in _all_imports()} | {'pytest'})
    with_names = {top: _is_stdlib(top) for top in tops}
    monkeypatch.delattr(sys, 'stdlib_module_names', raising=False)
    assert {top: _is_stdlib(top) for top in tops} == with_names
    assert with_names['threading'] and with_names['errno'] and not with_names['pytest']
