"""LUMI Orchestrator custom exceptions.

Domain-specific exceptions for the brief generation pipeline,
distinguishing between partial and total data failures.
"""


class DataPullError(Exception):
    """Base exception for data pull failures.

    Raised when a specific data source API call fails,
    identifying which source experienced the error.

    Args:
        source: The name of the data source that failed (e.g., "SPOG_XPMS").
        message: Human-readable description of the failure.
    """

    def __init__(self, source: str, message: str) -> None:
        super().__init__(f"[{source}] {message}")
        self.source = source


class PartialDataError(Exception):
    """Raised when some data sources failed but others succeeded.

    This allows the orchestrator to generate a partial brief with
    available data while flagging which sources were unavailable.

    Args:
        failed_sources: List of source names that failed.
        message: Optional description of the partial failure.
    """

    def __init__(self, failed_sources: list, message: str = "") -> None:
        self.failed_sources = failed_sources
        super().__init__(message or f"Partial data failure: {failed_sources}")


class AllSourcesFailedError(Exception):
    """Raised when all external data sources are unavailable.

    This triggers the cached-brief fallback path in the orchestrator.

    Args:
        message: Description of the total failure.
    """

    def __init__(self, message: str = "All SPOG/MDP data sources failed") -> None:
        super().__init__(message)


class BriefGenerationError(Exception):
    """Raised when the AI brief generation pipeline fails.

    Identifies which stage of the pipeline (template, bedrock, validation)
    encountered the error.

    Args:
        stage: Pipeline stage that failed (e.g., "bedrock", "validation").
        message: Description of what went wrong.
    """

    def __init__(self, stage: str, message: str) -> None:
        self.stage = stage
        super().__init__(f"Brief generation failed at {stage}: {message}")


class AudioSynthesisError(Exception):
    """Raised when Polly TTS synthesis fails.

    The orchestrator catches this to set the brief status to TEXT_ONLY
    and continue delivery without audio.

    Args:
        message: Description of the synthesis failure.
    """

    def __init__(self, message: str) -> None:
        super().__init__(f"Audio synthesis failed: {message}")
