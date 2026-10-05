"""Import shim exposing the LUMI ``dataset_generator`` package to the orchestrator.

The Task 1 re-anchor generators live under
``lumi/backend/functions/seed-data/dataset_generator/``. The orchestrator
Generate and Reconcile steps (Task 3) reuse those generators rather than
re-implementing generation. To import them without baking a fragile relative
path into every handler, this shim resolves the package location once and adds
it to ``sys.path``.

Resolution order (first hit wins):

1. ``DATASET_GENERATOR_PATH`` environment variable, if set. This is the runtime
   contract: the deployment packages the ``dataset_generator`` package alongside
   the orchestrator step handlers (or sets this variable to its location), so
   ``import dataset_generator`` resolves inside the Lambda without any path
   guessing. Tests may also set it (see the tests' ``conftest``).
2. A repo-relative fallback computed from this file's location
   (``.../shared/data-orchestrator/handlers`` ->
   ``.../lumi/backend/functions/seed-data``). This keeps local/unit runs working
   when the env var is absent, and is only a convenience fallback - production
   relies on the env var / packaged layout above.

Importing this module for its side effect (path registration) is enough; it
also re-exports the generator entry points for convenient, typed access.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import List

# Environment variable a deployment sets to the directory that contains the
# ``dataset_generator`` package. Preferred over any path guessing at runtime.
DATASET_GENERATOR_PATH_ENV = "DATASET_GENERATOR_PATH"


def _candidate_paths() -> List[Path]:
    """Return ordered candidate directories that may contain the package.

    Returns:
        A list of directories to probe for a ``dataset_generator`` package,
        most authoritative first (env var, then the repo-relative fallback).
    """
    candidates: List[Path] = []

    env_path = os.environ.get(DATASET_GENERATOR_PATH_ENV)
    if env_path:
        candidates.append(Path(env_path))

    # Repo-relative fallback: handlers dir -> repo root -> LUMI seed-data dir.
    # handlers/ -> data-orchestrator/ -> shared/ -> <repo root>. Guard the index:
    # in a packaged Lambda this file is at the zip root (the dataset_generator
    # package is vendored alongside it), so there are not enough parents - avoid
    # an IndexError at import time that would crash the whole handler.
    resolved = Path(__file__).resolve()
    if len(resolved.parents) > 3:
        repo_root = resolved.parents[3]
        candidates.append(repo_root / "lumi" / "backend" / "functions" / "seed-data")

    return candidates


def _ensure_dataset_generator_on_path() -> None:
    """Add the first directory that contains ``dataset_generator`` to sys.path.

    Idempotent: if a candidate directory is already on ``sys.path`` it is not
    duplicated. Does nothing if no candidate contains the package (the
    subsequent ``import dataset_generator`` will then raise a clear ImportError).
    """
    for candidate in _candidate_paths():
        package_marker = candidate / "dataset_generator" / "__init__.py"
        if package_marker.is_file():
            candidate_str = str(candidate)
            if candidate_str not in sys.path:
                # Insert at front so the packaged/real generators win over any
                # stale same-named module elsewhere on the path.
                sys.path.insert(0, candidate_str)
            return


_ensure_dataset_generator_on_path()

# Re-export the generator entry points so handlers can do
# ``from dataset_generator_shim import generate_rooms`` and get typed symbols
# once the path is registered above.
from dataset_generator import (  # noqa: E402  (import after path setup by design)
    BatchWriter,
    generate_guests,
    generate_reservations,
    generate_revenue,
    generate_rooms,
    generate_work_orders,
    reconcile_room_status,
    resolve_reference_date,
)

__all__ = [
    "BatchWriter",
    "generate_rooms",
    "generate_guests",
    "generate_revenue",
    "generate_reservations",
    "generate_work_orders",
    "reconcile_room_status",
    "resolve_reference_date",
    "DATASET_GENERATOR_PATH_ENV",
]
