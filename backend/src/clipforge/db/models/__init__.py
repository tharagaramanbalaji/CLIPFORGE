"""Importing this package registers every table on Base.metadata (Alembic autogenerate relies on it)."""

from clipforge.db.models.analysis import Scene, SignalSeries, Speaker, Transcript, TranscriptSegment
from clipforge.db.models.clips import AuditEvent, Clip, ClipVersion, SubtitleTrack
from clipforge.db.models.highlights import HighlightCandidate, LlmCall
from clipforge.db.models.media import (
    Artifact,
    ArtifactKind,
    MediaAsset,
    MediaStatus,
    Project,
    SourceType,
    UploadSession,
)
from clipforge.db.models.pipeline import (
    PipelineKind,
    PipelineRun,
    RunStatus,
    StageName,
    StageRun,
    StageStatus,
)
from clipforge.db.models.tenancy import (
    Creator,
    MemberRole,
    Membership,
    RightsBasis,
    RightsGrant,
    User,
    Workspace,
)

__all__ = [
    "Artifact",
    "ArtifactKind",
    "AuditEvent",
    "Clip",
    "ClipVersion",
    "Creator",
    "HighlightCandidate",
    "LlmCall",
    "MediaAsset",
    "MediaStatus",
    "MemberRole",
    "Membership",
    "PipelineKind",
    "PipelineRun",
    "Project",
    "RightsBasis",
    "RightsGrant",
    "RunStatus",
    "Scene",
    "SignalSeries",
    "SourceType",
    "Speaker",
    "StageName",
    "StageRun",
    "StageStatus",
    "SubtitleTrack",
    "Transcript",
    "TranscriptSegment",
    "UploadSession",
    "User",
    "Workspace",
]
