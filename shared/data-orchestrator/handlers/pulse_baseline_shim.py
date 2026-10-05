"""Import shim exposing the PULSE-owned baseline builder to the orchestrator.

Task 5 mechanism: the curated baseline is owned by PULSE (design "Component 5:
Curated baseline priming") and lives at
``pulse/backend/src/pulse/baseline/builder.py``. The orchestrator PrimeBaseline
step Lambda only *calls* that seam; it never re-implements the baseline itself.
To import the PULSE package without baking a fragile relative path into the
handler, this shim resolves the PULSE ``src`` layout once and adds it to
``sys.path`` -- mirroring ``pulse_quiesce_shim`` (the quiesce seam) and
``dataset_generator_shim`` (the LUMI generators).

Resolution order (first hit wins):

1. ``PULSE_BACKEND_SRC_PATH`` environment variable, if set. This is the runtime
   contract: the deployment packages the PULSE ``pulse`` package alongside the
   orchestrator step handlers, or points this variable at it, so
   ``import pulse.baseline.builder`` resolves inside the Lambda without path
   guessing. Shared with the quiesce shim so both PULSE seams resolve the same
   way.
2. A repo-relative fallback computed from this file's location
   (``.../shared/data-orchestrator/handlers`` -> ``.../pulse/backend/src``).
   Keeps local/unit runs working when the env var is absent.

The import is best-effort: if the PULSE package is not on the path (for example
in an isolated orchestrator-only unit test), :data:`SEAM_AVAILABLE` is ``False``
and the seam callable is ``None``. The handler then degrades to a structured
no-op so the scaffold contract still holds; the real builder is used whenever
PULSE is importable.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Callable, List, Optional

# Environment variable a deployment sets to the directory that contains the
# ``pulse`` package (the PULSE backend ``src`` layout root). Intentionally the
# same variable the quiesce shim reads, so one deployment setting wires both
# PULSE seams.
PULSE_BACKEND_SRC_PATH_ENV = "PULSE_BACKEND_SRC_PATH"


def _candidate_paths() -> List[Path]:
    """Return ordered candidate directories that may contain the ``pulse`` pkg.

    Returns:
        Directories to probe for a ``pulse`` package, most authoritative first
        (env var, then the repo-relative fallback).
    """
    candidates: List[Path] = []

    env_path = os.environ.get(PULSE_BACKEND_SRC_PATH_ENV)
    if env_path:
        candidates.append(Path(env_path))

    # Repo-relative fallback: handlers/ -> data-orchestrator/ -> shared/ ->
    # <repo root> -> pulse/backend/src. Guard the index: in a packaged Lambda
    # this file is at the zip root (the pulse package is vendored alongside it),
    # so there are not enough parents - avoid an IndexError at import time that
    # would crash the whole handler.
    resolved = Path(__file__).resolve()
    if len(resolved.parents) > 3:
        repo_root = resolved.parents[3]
        candidates.append(repo_root / "pulse" / "backend" / "src")

    return candidates


def _ensure_pulse_on_path() -> None:
    """Add the first directory that contains the ``pulse`` package to sys.path.

    Idempotent: a candidate already on ``sys.path`` is not duplicated. Does
    nothing when no candidate contains the package; the subsequent import then
    fails and the shim degrades to the unavailable state.
    """
    for candidate in _candidate_paths():
        package_marker = candidate / "pulse" / "baseline" / "builder.py"
        if package_marker.is_file():
            candidate_str = str(candidate)
            if candidate_str not in sys.path:
                sys.path.insert(0, candidate_str)
            return


_ensure_pulse_on_path()

# Best-effort import of the seam. Kept optional so orchestrator-only unit tests
# that do not vendor the PULSE package still import the handler cleanly.
prime_property_baseline: Optional[Callable[..., dict]] = None
BASELINE_ID_PREFIX: Optional[str] = None
SEAM_AVAILABLE = False

try:  # pragma: no cover - exercised via availability of the PULSE package
    from pulse.baseline.builder import BASELINE_ID_PREFIX as _BASELINE_ID_PREFIX
    from pulse.baseline.builder import prime_property_baseline as _prime

    prime_property_baseline = _prime
    BASELINE_ID_PREFIX = _BASELINE_ID_PREFIX
    SEAM_AVAILABLE = True
except ImportError:
    SEAM_AVAILABLE = False


__all__ = [
    "PULSE_BACKEND_SRC_PATH_ENV",
    "SEAM_AVAILABLE",
    "BASELINE_ID_PREFIX",
    "prime_property_baseline",
]
