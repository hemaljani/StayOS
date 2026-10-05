"""Import shim exposing the PULSE-owned quiesce seam to the orchestrator.

Task 4 mechanism: the quiesce / un-quiesce mechanism is owned by PULSE (design
"Component 3: PULSE quiesce seam") and lives at
``pulse/backend/src/pulse/rule_engine/quiesce.py``. The orchestrator Quiesce and
UnQuiesce step Lambdas only *call* that seam; they never implement suppression
themselves. To import the PULSE package without baking a fragile relative path
into the handlers, this shim resolves the PULSE ``src`` layout once and adds it
to ``sys.path`` -- mirroring ``dataset_generator_shim`` for the LUMI generators.

Resolution order (first hit wins):

1. ``PULSE_BACKEND_SRC_PATH`` environment variable, if set. This is the runtime
   contract: the deployment packages the PULSE ``pulse`` package (or the shared
   quiesce module) alongside the orchestrator step handlers, or points this
   variable at it, so ``import pulse.rule_engine.quiesce`` resolves inside the
   Lambda without path guessing.
2. A repo-relative fallback computed from this file's location
   (``.../shared/data-orchestrator/handlers`` -> ``.../pulse/backend/src``).
   Keeps local/unit runs working when the env var is absent.

The import is best-effort: if the PULSE package is not on the path (for example
in an isolated orchestrator-only unit test), :data:`SEAM_AVAILABLE` is ``False``
and the seam callables are ``None``. Callers then fall back to a structured
no-op so the scaffold contract still holds; the real seam is used whenever PULSE
is importable and the ESM UUIDs are configured.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Callable, List, Optional

# Environment variable a deployment sets to the directory that contains the
# ``pulse`` package (the PULSE backend ``src`` layout root).
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
    # <repo root> -> pulse/backend/src. Only applicable in the repo layout; in a
    # packaged Lambda this file sits at the zip root (the `pulse` package is
    # vendored alongside it and resolves via sys.path), so there are not enough
    # parents - guard the index so import never crashes (would otherwise raise
    # IndexError at import time and take the whole handler down).
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
        package_marker = candidate / "pulse" / "rule_engine" / "quiesce.py"
        if package_marker.is_file():
            candidate_str = str(candidate)
            if candidate_str not in sys.path:
                sys.path.insert(0, candidate_str)
            return


_ensure_pulse_on_path()

# Best-effort import of the seam. Kept optional so orchestrator-only unit tests
# that do not vendor the PULSE package still import the handlers cleanly.
quiesce_rule_engine: Optional[Callable[..., dict]] = None
unquiesce_rule_engine: Optional[Callable[..., dict]] = None
QuiesceError: Optional[type] = None
MECHANISM: Optional[str] = None
SEAM_AVAILABLE = False

try:  # pragma: no cover - exercised via availability of the PULSE package
    from pulse.rule_engine.quiesce import MECHANISM as _MECHANISM
    from pulse.rule_engine.quiesce import QuiesceError as _QuiesceError
    from pulse.rule_engine.quiesce import quiesce_rule_engine as _quiesce
    from pulse.rule_engine.quiesce import unquiesce_rule_engine as _unquiesce

    quiesce_rule_engine = _quiesce
    unquiesce_rule_engine = _unquiesce
    QuiesceError = _QuiesceError
    MECHANISM = _MECHANISM
    SEAM_AVAILABLE = True
except ImportError:
    SEAM_AVAILABLE = False


__all__ = [
    "PULSE_BACKEND_SRC_PATH_ENV",
    "SEAM_AVAILABLE",
    "MECHANISM",
    "QuiesceError",
    "quiesce_rule_engine",
    "unquiesce_rule_engine",
]
