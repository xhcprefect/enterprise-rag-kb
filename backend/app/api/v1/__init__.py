from fastapi import APIRouter

from app.api.v1 import (
    audit,
    auth,
    chat,
    feedback,
    feedback_regression,
    feedback_regression_jobs,
    kb_members,
    knowledge,
    operations,
    projects,
    usage,
    users,
)

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(kb_members.router)
api_router.include_router(knowledge.router)
api_router.include_router(chat.router)
api_router.include_router(feedback.router)
api_router.include_router(feedback_regression.router)
api_router.include_router(feedback_regression_jobs.router)
api_router.include_router(audit.router)
api_router.include_router(usage.router)
api_router.include_router(operations.router)
api_router.include_router(projects.router)
