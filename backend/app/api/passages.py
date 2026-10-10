from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, Query, UploadFile

from app.api.persons import MAX_PHOTO_BYTES
from app.dependency import ControllerDep, DbDep
from app.schemas.passages import (
    CodeIn, FaceResultOut, PassageCreate, PassagePersonOut, PassageStatusOut,
)
from app.services import passage as service

router = APIRouter(tags=["passages"])


@router.get("/passages/people", response_model=list[PassagePersonOut])
def find_people(db: DbDep, user: ControllerDep, q: Annotated[str | None, Query(max_length=100)] = None, limit: Annotated[int, Query(ge=1, le=50)] = 20):
    """Поиск человека по ФИО (после показа удостоверения). Объявлен до /passages/{session_id}."""
    return service.search_people(db, q, limit)


@router.post("/passages", response_model=PassageStatusOut)
def create_passage(body: PassageCreate, db: DbDep, user: ControllerDep):
    """Контролёр выбрал человека: сессия с одноразовым кодом (код видит только телефон рабочего)."""
    return service.create(db, user["sub"], body.person_id, body.direction)


@router.post("/passages/{session_id}/code", response_model=PassageStatusOut)
def enter_code(session_id: UUID, body: CodeIn, db: DbDep, user: ControllerDep):
    """Контролёр вводит 6-значный код, который назвал рабочий."""
    return service.submit_code(db, session_id, user["sub"], body.code)


@router.get("/passages/{session_id}", response_model=PassageStatusOut)
def get_passage(session_id: UUID, db: DbDep, user: ControllerDep):
    """Статус сессии; планшет опрашивает раз в секунду."""
    return service.get_status(db, session_id)


@router.post("/passages/{session_id}/face", response_model=FaceResultOut)
def check_face(
    session_id: UUID,
    db: DbDep,
    user: ControllerDep,
    frames: Annotated[list[UploadFile], File(min_length=1, max_length=5)],
):
    """1–5 кадров лица (multipart, поле frames); сервер сам решает allow/deny."""
    data, too_large = [], False
    for f in frames:
        content = f.file.read(MAX_PHOTO_BYTES + 1)  # лимит как у фото; остальные кадры после превышения не читаем
        data.append((content, f.filename or "frame.jpg", f.content_type or "application/octet-stream"))
        if len(content) > MAX_PHOTO_BYTES:
            too_large = True
            break
    return service.check_face(db, session_id, user["sub"], data, too_large)
