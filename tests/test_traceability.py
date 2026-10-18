"""Every requirement has a test (task 13.2; Requirement N7.1).

The IDs are the bold "**1.1**" that starts each requirement in the spec's
requirements.md. A test names the requirements it checks with
@pytest.mark.req("<id>", ...), on the test itself, its class, or a module's
`pytestmark`. The marks are read from the source of every test file under
tests/, tests/ui/ included, without importing or running anything, so the
browser tests count even in a run that deselects them. Whether the tests
pass is the full runs' job: `pytest -q`, and `pytest -q -m ui tests/ui`."""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = ROOT / ".kiro" / "specs" / "i-want-to-build-ai" / "requirements.md"
ID_LINE = re.compile(r"^\s*[-*]\s+\*\*(N?\d+\.\d+)\*\*\s", re.M)


def requirement_ids() -> list[str]:
    return ID_LINE.findall(REQUIREMENTS.read_text(encoding="utf-8"))


def _named(node) -> list[str]:
    """The IDs in every pytest.mark.req(...) inside `node`."""
    ids = []
    for call in ast.walk(node):
        func = getattr(call, "func", None)
        if (isinstance(call, ast.Call) and isinstance(func, ast.Attribute) and func.attr == "req"
                and isinstance(func.value, ast.Attribute) and func.value.attr == "mark"):
            ids += [arg.value for arg in call.args if isinstance(arg, ast.Constant) and isinstance(arg.value, str)]
    return ids


def _pytestmark(body) -> list[str]:
    return [i for node in body if isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets)
            for i in _named(node.value)]


def tagged() -> dict[str, list[str]]:
    """Each named requirement ID, with the tests naming it ("file::test")."""
    found: dict[str, list[str]] = {}

    def add(path: Path, name: str, ids) -> None:
        for i in ids:
            found.setdefault(i, []).append(f"{path.relative_to(ROOT).as_posix()}::{name}")

    for path in sorted((ROOT / "tests").rglob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        module = _pytestmark(tree.body)
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                add(path, node.name, module + [i for d in node.decorator_list for i in _named(d)])
            elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                shared = module + _pytestmark(node.body) + [i for d in node.decorator_list for i in _named(d)]
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test"):
                        add(path, f"{node.name}::{item.name}",
                            shared + [i for d in item.decorator_list for i in _named(d)])
    return found


@pytest.mark.req("N7.1")
def test_the_requirement_ids_are_read():
    ids = requirement_ids()
    assert len(ids) == len(set(ids)), "an ID appears twice in requirements.md"
    assert {"1.1", "14.4", "N1.1", "N7.1"} <= set(ids)


@pytest.mark.req("N7.1")
def test_every_requirement_has_a_test():
    named = tagged()
    missing = [i for i in requirement_ids() if i not in named]
    assert not missing, "No test is marked req(...) for: " + ", ".join(missing)


@pytest.mark.req("N7.1")
def test_every_mark_names_a_requirement():
    known = set(requirement_ids())
    unknown = {i: tests for i, tests in tagged().items() if i not in known}
    assert not unknown, f"req(...) names IDs that requirements.md doesn't have: {unknown}"
