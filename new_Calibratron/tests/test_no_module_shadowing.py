"""One static guard: no function may bind a name it also imports at module level.

This is not style policing. `calibrate_tier3` bound a local named `quality`
two-thirds of the way down, which made `quality.assess_adpd_sweep(...)` near the
*top* of the same function an UnboundLocalError - and because that call is only
reached when ADPD traces exist, it surfaced after a complete seven-point lamp
sweep, discarding the whole run. Python's function-wide scoping means the crash
site and the cause are arbitrarily far apart, so it is worth checking statically.
"""

import ast
import os

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULES = ["run_calibratron.py", "helpers.py", "spec_cal.py", "quality.py"]


def imported_names(tree):
    """Module-level import bindings: `import x`, `import x as y`, `from m import n`."""
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
    return names


def bound_names(fn):
    """Every name this function binds locally, by any statement that binds one."""
    names = {a.arg for a in (fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs)}
    for arg in (fn.args.vararg, fn.args.kwarg):
        if arg:
            names.add(arg.arg)
    for node in ast.walk(fn):
        if isinstance(node, ast.FunctionDef) and node is not fn:
            names.add(node.name)
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, ast.Global):
            names.difference_update(node.names)   # explicitly module-scoped
    return names


@pytest.mark.parametrize("filename", MODULES)
def test_no_function_shadows_an_imported_module(filename):
    path = os.path.join(HERE, filename)
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename)
    imports = imported_names(tree)
    globals_ = {n.names[0] for n in ast.walk(tree) if isinstance(n, ast.Global)}

    offenders = []
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        for name in sorted(bound_names(fn) & imports - globals_):
            offenders.append(f"{filename}:{fn.lineno} {fn.name}() binds {name!r}")
    assert not offenders, "\n".join(offenders)
