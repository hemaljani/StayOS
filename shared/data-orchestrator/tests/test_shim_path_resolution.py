"""Regression tests for shim path resolution at the packaged (zip-root) layout.

The import shims (dataset_generator_shim, pulse_quiesce_shim, pulse_baseline_shim)
compute a repo-relative fallback via ``Path(__file__).resolve().parents[3]``. In
a packaged Lambda the shim sits at the zip root, so it has fewer than 4 parents;
an unguarded ``parents[3]`` raises ``IndexError`` AT IMPORT TIME and crashes the
whole step handler (observed as an AI-8 roll-forward failure). These tests pin
that ``_candidate_paths`` is index-safe regardless of file depth.

# Feature: data-Orchestrator
Validates: AI-8 deploy fix (shim IndexError at zip-root layout).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import dataset_generator_shim
import pulse_baseline_shim
import pulse_quiesce_shim
import pytest


@pytest.mark.parametrize(
    "shim",
    [dataset_generator_shim, pulse_quiesce_shim, pulse_baseline_shim],
)
def test_candidate_paths_never_indexerror(shim: Any) -> None:
    """_candidate_paths returns without raising, for every shim."""
    candidates = shim._candidate_paths()
    assert isinstance(candidates, list)


@pytest.mark.parametrize(
    "shim",
    [dataset_generator_shim, pulse_quiesce_shim, pulse_baseline_shim],
)
def test_candidate_paths_index_safe_at_zip_root(
    shim: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulate the packaged (zip-root) layout: too few parents must not crash.

    Points the shim's ``__file__`` at a shallow path (2 parents) so the
    repo-relative ``parents[3]`` fallback is out of range; the guard must skip
    it and return the (possibly empty / env-only) candidate list rather than
    raising IndexError.
    """
    shallow = Path("/var/task/shim.py")
    monkeypatch.setattr(shim, "__file__", str(shallow))
    # Must not raise IndexError even though parents[3] is out of range.
    candidates = shim._candidate_paths()
    assert isinstance(candidates, list)
