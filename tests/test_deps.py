"""Does the repo declare what it imports, and does the core still run without the extras.

On an earlier project of mine a repo shipped with no requirements file at all,
including the database driver, so not one command in its README ran on a clean machine.
Nobody noticed for a week because the sandbox already had the package. This runs from the first
commit for that reason.
"""

from __future__ import annotations

import ast
import os
import sys
from typing import Dict, List, Set, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Declared here rather than derived, because the point is to catch a package that is
# installed in this sandbox and declared nowhere.
REQUIREMENTS = os.path.join(ROOT, "requirements.txt")


def _local_module_names() -> Set[str]:
    """Derived from the tree rather than written down.

    A hand written list breaks the first time a module is imported from beside its
    importer, and then the obvious fix is to widen the list until it stops complaining,
    at which point a repo file named `yaml.py` would hide a missing PyYAML declaration.
    """
    names = set()
    for entry in os.listdir(ROOT):
        path = os.path.join(ROOT, entry)
        if os.path.isdir(path) and os.path.exists(os.path.join(path, "__init__.py")):
            names.add(entry)
        elif entry.endswith(".py"):
            names.add(entry[:-3])
    # Modules that live beside a runner and get imported by bare name.
    for pkg in ("tests", "scripts", "pii"):
        d = os.path.join(ROOT, pkg)
        if not os.path.isdir(d):
            continue
        names.add(pkg)
        for entry in os.listdir(d):
            if entry.endswith(".py") and entry != "__init__.py":
                names.add(entry[:-3])
    return names


def _python_files() -> List[str]:
    out = []
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "build", ".venv")]
        for f in files:
            if f.endswith(".py"):
                out.append(os.path.join(base, f))
    return sorted(out)


def _top_level(name: str) -> str:
    return name.split(".")[0]


def _imports(tree: ast.AST, body_only: bool) -> Set[str]:
    """Top level package names imported by this module.

    `body_only` reads `tree.body` rather than walking the whole tree, which is the
    difference between "what happens when this module is imported" and "what appears
    anywhere in the file". An import inside a function is a deferred one and demanding it
    of every caller is wrong.
    """
    nodes = tree.body if body_only else list(ast.walk(tree))
    found = set()
    for node in nodes:
        if isinstance(node, ast.Import):
            for a in node.names:
                found.add(_top_level(a.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                found.add(_top_level(node.module))
    return found


def _declared() -> Set[str]:
    names = set()
    with open(REQUIREMENTS) as fh:
        for line in fh:
            line = line.split("#")[0].strip()
            if not line:
                continue
            for sep in ("==", ">=", "<=", "~=", ">", "<", "["):
                if sep in line:
                    line = line.split(sep)[0]
            names.add(line.strip().lower().replace("-", "_"))
    return names


def _third_party(path: str, body_only: bool) -> Set[str]:
    with open(path) as fh:
        tree = ast.parse(fh.read(), filename=path)
    local = _local_module_names()
    stdlib = set(sys.stdlib_module_names)
    return {
        m for m in _imports(tree, body_only)
        if m not in stdlib and m not in local and not m.startswith("_")
    }


def check_a_requirements_file_exists_and_is_not_empty():
    assert os.path.exists(REQUIREMENTS), "no requirements.txt"
    assert _declared(), "requirements.txt declares nothing"


def check_every_third_party_import_anywhere_in_the_repo_is_declared():
    declared = _declared()
    undeclared: Dict[str, List[str]] = {}
    for path in _python_files():
        for mod in _third_party(path, body_only=False):
            if mod.lower().replace("-", "_") not in declared:
                undeclared.setdefault(mod, []).append(
                    os.path.relpath(path, ROOT))
    assert not undeclared, "undeclared: {}".format(undeclared)


def check_the_library_imports_nothing_third_party_at_module_level():
    # The core suite has to run on a clean machine with nothing installed. If a module
    # under pii/ ever imports the database driver at the top, that stops being true
    # silently, because the sandbox has it.
    for path in _python_files():
        rel = os.path.relpath(path, ROOT)
        if not rel.startswith("pii" + os.sep):
            continue
        third = _third_party(path, body_only=True)
        assert not third, "{} imports {} at module level".format(rel, sorted(third))


def check_the_core_runner_names_no_module_that_needs_the_driver():
    import tests.run_all as run_all
    for name in run_all.MODULES:
        rel = os.path.join(ROOT, *name.split(".")) + ".py"
        assert os.path.exists(rel), name
        assert "duckdb" not in _third_party(rel, body_only=False), name


def check_the_body_only_reading_really_differs_from_the_whole_tree_reading():
    # A distinction nothing exercises is a distinction that is not being tested. There has
    # to be at least one file in the repo with a deferred third party import, or the two
    # readings above are the same check written twice.
    deferred = []
    for path in _python_files():
        whole = _third_party(path, body_only=False)
        top = _third_party(path, body_only=True)
        if whole - top:
            deferred.append((os.path.relpath(path, ROOT), sorted(whole - top)))
    assert deferred, "nothing in the repo defers a third party import"


def check_the_requirements_parser_survives_the_shapes_a_requirements_file_carries():
    import tempfile
    body = "\n".join([
        "# a comment",
        "",
        "duckdb==1.5.5",
        "something>=2.0  # trailing comment",
        "other[extra]~=1.2",
        "Bare-Name",
    ])
    global REQUIREMENTS
    original = REQUIREMENTS
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
        fh.write(body)
        tmp = fh.name
    try:
        REQUIREMENTS = tmp
        assert _declared() == {"duckdb", "something", "other", "bare_name"}, _declared()
    finally:
        REQUIREMENTS = original
        os.unlink(tmp)


def check_the_local_module_set_is_derived_and_covers_the_packages_here():
    local = _local_module_names()
    for expected in ("pii", "tests", "taxonomy", "runner", "coverage_probe"):
        assert expected in local, expected
    assert "duckdb" not in local


def check_every_python_file_parses():
    for path in _python_files():
        with open(path) as fh:
            ast.parse(fh.read(), filename=path)


def check_the_import_reader_finds_both_import_forms():
    tree = ast.parse("import a.b\nfrom c.d import e\nfrom . import f\n")
    found = _imports(tree, body_only=True)
    assert found == {"a", "c"}, found


def check_the_import_reader_does_not_climb_into_a_function_when_asked_not_to():
    src = "import os\n\n\ndef f():\n    import duckdb\n    return duckdb\n"
    tree = ast.parse(src)
    assert _imports(tree, body_only=True) == {"os"}
    assert _imports(tree, body_only=False) == {"os", "duckdb"}


def check_stdlib_and_local_names_are_both_excluded() -> None:
    import tempfile
    src = "import os\nimport json\nimport pii\nimport duckdb\n"
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
        fh.write(src)
        tmp = fh.name
    try:
        assert _third_party(tmp, body_only=True) == {"duckdb"}
    finally:
        os.unlink(tmp)


def check_the_undeclared_detector_can_fail() -> Tuple:
    # The control for the declaration check. Without it, a parser returning an empty set
    # on everything would report the repo clean forever.
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
        fh.write("import a_package_nobody_declared\n")
        tmp = fh.name
    try:
        found = _third_party(tmp, body_only=True)
        assert found == {"a_package_nobody_declared"}
        assert not found.issubset(_declared())
    finally:
        os.unlink(tmp)
    return ()
