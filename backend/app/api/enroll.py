from fastapi import APIRouter

from app.dependency import AdminDep, DbDep
from app.schemas.enroll import EnrollCodeOut, EnrollIn, EnrollOut
from app.services import enroll as service

router = APIRouter(tags=["enroll"])


@router.post("/persons/{person_id}/enroll-code", response_model=EnrollCodeOut)
def create_enroll_code(person_id: int, db: DbDep, user: AdminDep):
    """Выдаёт одноразовый код активации телефона (гасит прежние неиспользованные)."""
    return service.create_code(db, user["sub"], person_id)


@router.post("/enroll", response_model=EnrollOut)
def enroll(body: EnrollIn, db: DbDep):
    """Без авторизации: рабочий отдаёт публичный ключ телефона и одноразовый код."""
    return service.enroll(db, body.code, body.public_key_jwk)
