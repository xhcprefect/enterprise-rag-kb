from app.models.audit import AuditLog
from app.models.conversation import (
    CONVERSATION_MODE_CHAT,
    CONVERSATION_MODE_RAG,
    CONVERSATION_MODES,
    Conversation,
    ChatTurn,
    Message,
)
from app.models.document import Document, DocumentChunk, DocumentStatus, KnowledgeBase
from app.models.feedback import (
    FEEDBACK_CATEGORIES,
    FEEDBACK_STATUSES,
    AnswerFeedback,
)
from app.models.feedback_regression import (
    REGRESSION_PROTOCOL_VERSION,
    FeedbackRegression,
    FeedbackRegressionRun,
)
from app.models.feedback_regression_job import (
    ACTIVE_JOB_STATUSES,
    EXECUTION_KIND_MANUAL,
    EXECUTION_KIND_ONLINE,
    EXECUTION_KINDS,
    JOB_STATUS_CANCELLED,
    JOB_STATUS_FAILED,
    JOB_STATUS_QUEUED,
    JOB_STATUS_RUNNING,
    JOB_STATUS_SUCCEEDED,
    JOB_STATUSES,
    REVIEW_STATUS_REJECTED,
    REVIEW_STATUS_UNKNOWN,
    REVIEW_STATUS_VERIFIED,
    REVIEW_STATUSES,
    TERMINAL_JOB_STATUSES,
    FeedbackRegressionBatch,
    FeedbackRegressionJob,
    FeedbackRegressionJobBody,
)
from app.models.knowledge_base_member import (
    KB_MEMBER_ROLE_MAINTAINER,
    KB_MEMBER_ROLE_READER,
    KB_MEMBER_ROLES,
    KnowledgeBaseMember,
    KnowledgeBaseMemberRole,
)
from app.models.memory import ConversationMemory, SummaryJob, SummaryJobStatus
from app.models.project import (
    ProjectChunk,
    ProjectFileParseStatus,
    ProjectFileVersion,
    ProjectSnapshot,
    ProjectSnapshotFile,
    ProjectSnapshotStatus,
    ProjectSource,
)
from app.models.upload import UploadSession, UploadStatus
from app.models.user import User, UserRole
from app.models.usage import ModelPrice, ModelUsage, UsageTrackingState
from app.models.worker import WorkerHeartbeat

# 知识库资源类型取值；供迁移脚本与接口共用。
KNOWLEDGE_BASE_KIND_UPLOAD = "upload"
KNOWLEDGE_BASE_KIND_PROJECT = "project"
KNOWLEDGE_BASE_KINDS = (KNOWLEDGE_BASE_KIND_UPLOAD, KNOWLEDGE_BASE_KIND_PROJECT)

__all__ = [
    "AuditLog",
    "CONVERSATION_MODE_CHAT",
    "CONVERSATION_MODE_RAG",
    "CONVERSATION_MODES",
    "Conversation",
    "ChatTurn",
    "Message",
    "ConversationMemory",
    "SummaryJob",
    "SummaryJobStatus",
    "AnswerFeedback",
    "FeedbackRegression",
    "FeedbackRegressionRun",
    "REGRESSION_PROTOCOL_VERSION",
    "FeedbackRegressionBatch",
    "FeedbackRegressionJob",
    "FeedbackRegressionJobBody",
    "ACTIVE_JOB_STATUSES",
    "EXECUTION_KIND_MANUAL",
    "EXECUTION_KIND_ONLINE",
    "EXECUTION_KINDS",
    "JOB_STATUS_CANCELLED",
    "JOB_STATUS_FAILED",
    "JOB_STATUS_QUEUED",
    "JOB_STATUS_RUNNING",
    "JOB_STATUS_SUCCEEDED",
    "JOB_STATUSES",
    "REVIEW_STATUS_REJECTED",
    "REVIEW_STATUS_UNKNOWN",
    "REVIEW_STATUS_VERIFIED",
    "REVIEW_STATUSES",
    "TERMINAL_JOB_STATUSES",
    "Document",
    "DocumentChunk",
    "DocumentStatus",
    "FEEDBACK_CATEGORIES",
    "FEEDBACK_STATUSES",
    "KnowledgeBase",
    "KB_MEMBER_ROLE_MAINTAINER",
    "KB_MEMBER_ROLE_READER",
    "KB_MEMBER_ROLES",
    "KnowledgeBaseMember",
    "KnowledgeBaseMemberRole",
    "KNOWLEDGE_BASE_KIND_UPLOAD",
    "KNOWLEDGE_BASE_KIND_PROJECT",
    "KNOWLEDGE_BASE_KINDS",
    "ProjectChunk",
    "ProjectFileParseStatus",
    "ProjectFileVersion",
    "ProjectSnapshot",
    "ProjectSnapshotFile",
    "ProjectSnapshotStatus",
    "ProjectSource",
    "UploadSession",
    "UploadStatus",
    "User",
    "UserRole",
    "ModelPrice",
    "ModelUsage",
    "UsageTrackingState",
    "WorkerHeartbeat",
]
