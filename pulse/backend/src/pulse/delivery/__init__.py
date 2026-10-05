"""Delivery layer components (``pulse-push-service`` and ``pulse-info-batcher``).

Delivers alerts over two channels: AWS AppSync Events for foreground/in-app
realtime updates, and Web Push (VAPID) for background wake-up. Also owns INFO
alert batching.

Implementation is added in later tasks; this module currently only marks the
sub-package.
"""

__all__: list[str] = []
