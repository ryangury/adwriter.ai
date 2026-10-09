"""A name used but never imported or defined is a NameError waiting for the one
code path that uses it (run_daily_report's EMAIL_ADS_READY slipped through the
suite that way). This checks every module that is not itself a test: any Name
that is loaded somewhere but bound nowhere in its file (assignment, def, class,
import, argument, except-as, comprehension / for target) and is not a builtin.
It is deliberately loose (a name bound anywhere in the file counts), so it only
catches names with no binding at all."""
import ast
import builtins
import unittest
from pathlib import Path

import _paths

SKIP = {"scratchpad"}


def unbound(path: Path) -> list[tuple[str, int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    bound = set(dir(builtins)) | {"__file__", "__name__", "__doc__"}
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(n.name)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            bound.add(n.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            bound.update((a.asname or a.name).split(".")[0] for a in n.names)
        elif isinstance(n, ast.arg):
            bound.add(n.arg)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            bound.add(n.name)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            bound.update(n.names)
    return sorted({(n.id, n.lineno) for n in ast.walk(tree)
                   if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id not in bound})


class UndefinedNames(unittest.TestCase):
    def test_no_module_uses_a_name_it_never_binds(self):
        bad = {}
        for path in sorted(_paths.ROOT.glob("*.py")):
            found = unbound(path)
            if found:
                bad[path.name] = found
        self.assertEqual(bad, {})


if __name__ == "__main__":
    unittest.main()
