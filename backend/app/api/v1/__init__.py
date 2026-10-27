from fastapi import APIRouter

from app.api.v1 import audit, auth, chat, knowledge, usage, users

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(knowledge.router)
api_router.include_router(chat.router)
api_router.include_router(audit.router)
api_router.include_router(usage.router)
