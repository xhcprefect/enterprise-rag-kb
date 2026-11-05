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
    "Document",
    "DocumentChunk",
    "DocumentStatus",
    "KnowledgeBase",
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
]
