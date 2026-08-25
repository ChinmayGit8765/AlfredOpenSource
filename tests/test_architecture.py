"""Executable architecture rules.

CLAUDE.md and ARCHITECTURE.md state the contracts the rest of this system
leans on: the domain does no I/O, time arrives through a port, every
structured call and every tool call goes through one chokepoint. A rule
that lives only in prose survives exactly until the first plausible
looking patch, so each one is asserted here against the parsed source.

These tests read files; they never import the modules they judge, so a
violation shows up as a named file and line rather than an import error.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ALFRED = REPO_ROOT / "alfred"
DOMAIN = ALFRED / "domain"
PORTS = ALFRED / "ports"

# The composition root. The one module allowed to know both sides.
COMPOSITION = ALFRED / "runtime" / "composition.py"


def _python_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imported_modules(tree: ast.Module) -> list[tuple[str, int]]:
    """Every module name this file imports, with the line it happens on."""
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.append((node.module, node.lineno))
    return found


def _rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


# ---------------------------------------------------------------------------
# The domain layer is pure
# ---------------------------------------------------------------------------

# Everything the domain is allowed to reach for, by top-level module name.
# An allowlist rather than a denylist on purpose: a new I/O library added to
# a domain module should fail here without anyone remembering to ban it.
DOMAIN_ALLOWED_ROOTS = frozenset(
    {
        "__future__",
        # stdlib, none of which touches the outside world
        "asyncio",
        "collections",
        "contextlib",
        "dataclasses",
        "datetime",
        "enum",
        "json",
        "logging",
        "math",
        "re",
        "typing",
        "uuid",
        # the one third-party dependency the domain is permitted: schemas
        "pydantic",
    }
)

# Inward-facing alfred packages the domain may import. Notably absent:
# alfred.adapters, alfred.runtime, alfred.config.
DOMAIN_ALLOWED_ALFRED = frozenset(
    {"alfred.domain", "alfred.ports", "alfred.errors", "alfred.testing"}
)


@pytest.mark.parametrize("path", _python_files(DOMAIN), ids=_rel)
def test_domain_imports_stay_inside_the_allowlist(path: Path) -> None:
    violations = []
    for module, line in _imported_modules(_parse(path)):
        if module.startswith("alfred"):
            package = ".".join(module.split(".")[:2])
            if package not in DOMAIN_ALLOWED_ALFRED:
                violations.append(f"{_rel(path)}:{line} imports {module}")
            continue
        if module.split(".")[0] not in DOMAIN_ALLOWED_ROOTS:
            violations.append(f"{_rel(path)}:{line} imports {module}")

    assert not violations, (
        "the domain layer must stay pure: no adapters, no runtime, no config, "
        "no I/O libraries. Route the effect through a port instead.\n"
        + "\n".join(violations)
    )


@pytest.mark.parametrize("path", _python_files(PORTS), ids=_rel)
def test_ports_depend_on_nothing_inside_alfred_but_each_other(path: Path) -> None:
    violations = [
        f"{_rel(path)}:{line} imports {module}"
        for module, line in _imported_modules(_parse(path))
        if module.startswith("alfred") and not module.startswith("alfred.ports")
    ]
    assert not violations, (
        "a port is a bare protocol; it cannot depend on the domain, an "
        "adapter, or the runtime.\n" + "\n".join(violations)
    )


# Wall-clock and monotonic calls the domain must never make directly. Time is
# a dependency here, injected through ClockPort, because a system that plans
# weeks has to be testable at an arbitrary "now".
FORBIDDEN_TIME_CALLS = {
    ("datetime", "now"),
    ("datetime", "utcnow"),
    ("datetime", "today"),
    ("date", "today"),
    ("time", "time"),
    ("time", "monotonic"),
    ("time", "time_ns"),
}


@pytest.mark.parametrize("path", _python_files(DOMAIN), ids=_rel)
def test_domain_reads_time_only_through_the_clock_port(path: Path) -> None:
    violations = []
    for node in ast.walk(_parse(path)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Name):
            continue
        if (func.value.id, func.attr) in FORBIDDEN_TIME_CALLS:
            violations.append(
                f"{_rel(path)}:{node.lineno} calls {func.value.id}.{func.attr}()"
            )

    assert not violations, (
        "the domain must take 'now' from the injected ClockPort; a direct "
        "clock read makes the behaviour untestable and timezone-dependent.\n"
        + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# Single chokepoints
# ---------------------------------------------------------------------------


def test_only_the_composition_root_names_an_adapter() -> None:
    """Adapters are constructed in exactly one module.

    Importing a plain type from an adapter module (a status dataclass, say)
    stays legal; importing the adapter class itself is the wiring act this
    rule is about, and it belongs in composition.py.
    """
    violations = []
    for path in _python_files(ALFRED):
        if path == COMPOSITION:
            continue
        for node in ast.walk(_parse(path)):
            if not isinstance(node, ast.ImportFrom):
                continue
            if not (node.module or "").startswith("alfred.adapters"):
                continue
            violations.extend(
                f"{_rel(path)}:{node.lineno} imports adapter {alias.name}"
                for alias in node.names
                if alias.name.endswith("Adapter")
            )

    assert not violations, (
        "adapters are wired in alfred/runtime/composition.py and nowhere "
        "else; that is what keeps the dependency arrows pointing inward.\n"
        + "\n".join(violations)
    )


def test_structured_output_goes_through_structured_call() -> None:
    """ModelPort.complete has exactly one caller outside the adapter layer.

    Every other path to the model goes through structured_call, so schema
    validation and the repair retry can never be skipped by a new feature
    that just wants "a bit of text back".
    """
    allowed = DOMAIN / "structured.py"
    violations = []
    for path in _python_files(ALFRED):
        if path == allowed or path.is_relative_to(ALFRED / "adapters"):
            continue
        if path.is_relative_to(PORTS) or path.is_relative_to(ALFRED / "testing"):
            continue
        for node in ast.walk(_parse(path)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "complete"
            ):
                violations.append(f"{_rel(path)}:{node.lineno} calls .complete()")

    assert not violations, (
        "raw model calls bypass schema validation and the repair retry; go "
        "through domain.structured.structured_call with a pydantic schema.\n"
        + "\n".join(violations)
    )


def test_tool_invocation_goes_through_the_dispatcher() -> None:
    """ToolPort.invoke is called by the dispatcher, not by agent paths.

    The dispatcher is where the per-agent allowlist, the capability tier
    gate, and the audit record live. A direct invoke would skip all three.
    """
    allowed = {DOMAIN / "dispatch.py", COMPOSITION}
    violations = []
    for path in _python_files(ALFRED):
        if path in allowed or path.is_relative_to(ALFRED / "adapters"):
            continue
        if path.is_relative_to(PORTS) or path.is_relative_to(ALFRED / "testing"):
            continue
        for node in ast.walk(_parse(path)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "invoke"
            ):
                violations.append(f"{_rel(path)}:{node.lineno} calls .invoke()")

    assert not violations, (
        "tool calls go through domain.dispatch.ToolDispatcher, which owns "
        "the allowlist, the capability gate, and the audit trail.\n"
        + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# Store discipline
# ---------------------------------------------------------------------------

STORE_METHODS = frozenset({"put", "get", "delete", "append", "query"})


def _is_store_receiver(node: ast.expr) -> bool:
    """True when this expression looks like the injected store.

    Deliberately name-based: a type checker cannot follow a Protocol through
    a constructor argument, and "does the receiver read as a store" catches
    the mistake this rule exists to stop without flagging every dict .get().
    """
    if isinstance(node, ast.Name):
        return "store" in node.id.lower()
    if isinstance(node, ast.Attribute):
        return "store" in node.attr.lower()
    return False


def test_collection_names_come_from_the_collections_registry() -> None:
    """No store call names its collection with a bare string literal.

    A typo'd collection name does not raise; it silently reads and writes an
    empty parallel universe, which is the worst possible failure for a system
    whose whole value is remembering things.
    """
    violations = []
    for path in _python_files(ALFRED):
        for node in ast.walk(_parse(path)):
            if not isinstance(node, ast.Call) or not isinstance(
                node.func, ast.Attribute
            ):
                continue
            if node.func.attr not in STORE_METHODS:
                continue
            if not _is_store_receiver(node.func.value):
                continue
            if not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                violations.append(
                    f"{_rel(path)}:{node.lineno} passes the literal "
                    f"{first.value!r} where a Collections member belongs"
                )

    assert not violations, (
        "collection names come from schemas.Collections; a bare string that "
        "drifts by one character fails silently forever.\n" + "\n".join(violations)
    )
