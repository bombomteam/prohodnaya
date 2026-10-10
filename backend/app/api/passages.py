from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, UploadFile

from app.api.persons import MAX_PHOTO_BYTES
from app.dependency import ControllerDep, DbDep
from app.schemas.passages import (
    FaceResultOut, PassageCreate, PassageCreated, PassageStatusOut, SignIn, SignOut,
)
from app.services import passage as service

router = APIRouter(tags=["passages"])


@router.post("/passages", response_model=PassageCreated)
def create_passage(body: PassageCreate, db: DbDep, user: ControllerDep):
    """Контролёр начинает проход: сессия с одноразовым nonce и QR."""
    return service.create(db, user["sub"], body.direction)


@router.post("/passages/{session_id}/sign", response_model=SignOut)
def sign_passage(session_id: UUID, body: SignIn, db: DbDep):
    """Без токена: телефон подписывает session_id|nonce|direction ключом устройства."""
    return service.sign(db, session_id, body)


@router.get("/passages/{session_id}", response_model=PassageStatusOut)
def get_passage(session_id: UUID, db: DbDep, user: ControllerDep):
    """Статус сессии; планшет опрашивает раз в секунду."""
    return service.get_status(db, session_id)


@router.post("/passages/{session_id}/face", response_model=FaceResultOut)
def check_face(
    session_id: UUID,
    db: DbDep,
    user: ControllerDep,
    frames: Annotated[list[UploadFile], File(min_length=1, max_length=10)],
):
    """1–10 кадров лица (multipart, поле frames); сервер сам решает allow/deny."""
    data, too_large = [], False
    for f in frames:
        content = f.file.read(MAX_PHOTO_BYTES + 1)  # лимит как у фото; остальные кадры после превышения не читаем
        data.append((content, f.filename or "frame.jpg", f.content_type or "application/octet-stream"))
        if len(content) > MAX_PHOTO_BYTES:
            too_large = True
            break
    return service.check_face(db, session_id, user["sub"], data, too_large)
