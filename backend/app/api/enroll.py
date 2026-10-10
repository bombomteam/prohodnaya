from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import APIRouter

from app.core.config import settings
from app.dependency import AdminDep
from app.schemas.common import PersonRef
from app.schemas.enroll import EnrollCodeOut, EnrollIn, EnrollOut

router = APIRouter(tags=["enroll"])


@router.post("/persons/{person_id}/enroll-code", response_model=EnrollCodeOut)
def create_enroll_code(person_id: int, user: AdminDep):
    """Выдаёт одноразовый код активации телефона (гасит прежние неиспользованные)."""
    # STUB
    code = "stub-code-" + "x" * 14
    return EnrollCodeOut(
        code=code,
        enroll_url=f"{settings.public_app_url}/enroll?code={code}",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=settings.enroll_code_ttl_min),
    )


@router.post("/enroll", response_model=EnrollOut)
def enroll(body: EnrollIn):
    """Без авторизации: рабочий отдаёт публичный ключ телефона и одноразовый код."""
    # STUB
    return EnrollOut(device_id=uuid4(), person=PersonRef(id=1, fio="Иванов Иван Иванович"))
