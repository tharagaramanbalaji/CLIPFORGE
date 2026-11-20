"""Shared exception hierarchy.

`retryable` tells workers whether a failure is transient (retry with backoff)
or permanent (fail the stage with a user-readable message).
"""

from __future__ import annotations


class ClipForgeError(Exception):
    retryable: bool = False

    def __init__(self, message: str, *, details: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ConfigError(ClipForgeError):
    pass


class ValidationFailed(ClipForgeError):
    pass


class ExternalToolError(ClipForgeError):
    """A subprocess (ffmpeg, ffprobe, ...) failed."""


class ExternalToolTimeout(ExternalToolError):
    retryable = True


class StorageError(ClipForgeError):
    pass


class NotFound(ClipForgeError):
    pass


class PermissionDenied(ClipForgeError):
    pass


class RightsNotGranted(PermissionDenied):
    """Upload/render attempted without a valid, unrevoked rights grant for the creator."""


class Conflict(ClipForgeError):
    pass


class Gone(ClipForgeError):
    pass


class Canceled(ClipForgeError):
    """The pipeline run was canceled while a stage was working."""
