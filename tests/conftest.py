"""Shared test fixtures.

Two things shape this file.

**Everything goes through the headless path.** REQ-T2 requires the suite to run
unattended with no GUI interaction, and the server's virtualenv has no GDAL or
PyQGIS by design (``design.md`` §2.2) -- so a test cannot open a fixture
directly even if it wanted to. Fixture properties are therefore asserted by
executing PyQGIS in a child process, which means every assertion about the
fixtures is also an assertion about the execution path that reads them.

**The context is session-scoped.** A QGIS startup costs roughly two seconds and
each child pays one. Sharing the staged payload across the session keeps that to
the unavoidable per-call cost rather than adding a directory copy to each.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO_ROOT / "test-data" / "generated"
TRUTH_PATH = REPO_ROOT / "test-data" / "truth.py"

if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))


def _load_truth():
    """Import ``test-data/truth.py`` by path.

    The directory is hyphenated and deliberately not a package -- it holds data
    and the generator, not importable library code -- so this loads the one
    module that tests need rather than making the whole directory importable.
    """
    spec = importlib.util.spec_from_file_location("fixture_truth", TRUTH_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


truth = _load_truth()


def pytest_report_header(config) -> str:
    """Say up front whether the fixtures are present.

    A suite that skips everything because fixtures were never generated looks
    identical to a suite with nothing to run.
    """
    if FIXTURE_DIR.exists():
        count = len(list(FIXTURE_DIR.glob("*")))
        return f"fixtures: {FIXTURE_DIR} ({count} files)"
    return f"fixtures: MISSING -- run `python test-data/make_fixtures.py`"


@pytest.fixture(scope="session")
def fixture_truth():
    """The declared fixture properties (``test-data/truth.py``)."""
    return truth


@pytest.fixture(scope="session")
def fixture_dir() -> Path:
    """Directory holding the generated fixtures."""
    if not FIXTURE_DIR.is_dir():
        pytest.skip(
            f"fixtures not generated. Run: python test-data/make_fixtures.py"
        )
    return FIXTURE_DIR


@pytest.fixture(scope="session")
def fixture_project(fixture_dir: Path) -> Path:
    path = fixture_dir / truth.PROJECT_NAME
    if not path.exists():
        pytest.skip(
            f"{path.name} missing. Run: python test-data/make_fixtures.py"
        )
    return path


@pytest.fixture(scope="session")
def fixture_dem(fixture_dir: Path) -> Path:
    path = fixture_dir / truth.DEM_NAME
    if not path.exists():
        pytest.skip(f"{path.name} missing. Run: python test-data/make_fixtures.py")
    return path


@pytest.fixture(scope="session")
def fixture_gpkg(fixture_dir: Path) -> Path:
    path = fixture_dir / truth.VECTOR_GPKG_NAME
    if not path.exists():
        pytest.skip(f"{path.name} missing. Run: python test-data/make_fixtures.py")
    return path


@pytest.fixture(scope="session")
def config():
    """Validated configuration, or a skip explaining why there is none."""
    from qgis_mcp_server.config import Config
    from qgis_mcp_server.qgis_install import QgisNotFound

    resolved = Config.load()
    try:
        resolved.validate()
    except QgisNotFound as exc:
        pytest.skip(f"no usable QGIS install: {exc}")
    return resolved


@pytest.fixture(scope="session")
def context(config):
    """A ``ServerContext`` shared across the session."""
    from qgis_mcp_server.context import ServerContext

    with ServerContext(config) as ctx:
        yield ctx


@pytest.fixture(scope="session")
def runner(context):
    """The headless runner."""
    return context.headless


# ---------------------------------------------------------------------------
# Assertion helpers (REQ-T3)
# ---------------------------------------------------------------------------


def describe(payload) -> str:
    """Render a structured payload for a failure message.

    REQ-T3 requires a failure to report expected against actual *structured*
    values, so that a failing run is diagnosable from its output without
    re-running the case by hand. These helpers put the whole payload in the
    message rather than leaving the reader with a bare ``assert False``.
    """
    import json

    return json.dumps(payload, indent=2, default=str)


def assert_ok(envelope, *, context_note: str = ""):
    """Assert an envelope succeeded, reporting the full error if it did not."""
    if envelope.ok:
        return envelope
    note = f" ({context_note})" if context_note else ""
    raise AssertionError(
        f"expected a successful envelope{note}, got a failure.\n"
        f"error:\n{describe(envelope.error)}\n"
        f"diagnostics:\n{envelope.diagnostics or '(none)'}"
    )


def assert_failed(envelope, expected_code: str, *, context_note: str = ""):
    """Assert an envelope failed with ``expected_code``."""
    note = f" ({context_note})" if context_note else ""
    if envelope.ok:
        raise AssertionError(
            f"expected failure code {expected_code!r}{note}, but the call "
            f"succeeded.\nresult:\n{describe(envelope.result)}\n"
            f"meta:\n{describe(envelope.meta)}"
        )
    actual_code = (envelope.error or {}).get("code")
    if actual_code != expected_code:
        raise AssertionError(
            f"expected failure code{note}\n"
            f"    expected: {expected_code!r}\n"
            f"    actual:   {actual_code!r}\n"
            f"full error:\n{describe(envelope.error)}"
        )
    return envelope


def assert_result(envelope, expected, *, label: str = "result"):
    """Assert the returned ``result`` equals ``expected``."""
    assert_ok(envelope, context_note=label)
    if envelope.result != expected:
        raise AssertionError(
            f"{label} mismatch\n"
            f"    expected: {describe(expected)}\n"
            f"    actual:   {describe(envelope.result)}"
        )
    return envelope
