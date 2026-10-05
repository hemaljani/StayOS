"""Action Executor component (``pulse-action-executor``).

Turns a GM-approved ranked option into a real write-back mutation on the
operational tables that clears the triggering condition, and resolves the
originating alert transactionally, closing the operational loop.

Implementation is added in later tasks; this module currently only marks the
sub-package.
"""

__all__: list[str] = []
