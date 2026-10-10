import json
from datetime import datetime, timedelta, timezone
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, File, UploadFile

from app.core.config import settings
from app.dependency import ControllerDep
from app.schemas.common import PersonRef, ShiftOut
from app.schemas.passages import (
    FaceResultOut, PassageCreate, PassageCreated, PassageStatusOut, SignedPerson, SignIn, SignOut,
)

router = APIRouter(tags=["passages"])

_FIO = "Иванов Иван Иванович"


@router.post("/passages", response_model=PassageCreated)
def create_passage(body: PassageCreate, user: ControllerDep):
    """Контролёр начинает проход: сессия с одноразовым nonce и QR."""
    # STUB
    sid, nonce = uuid4(), "stub-nonce"
    return PassageCreated(
        session_id=sid, nonce=nonce, direction=body.direction,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=settings.pass_session_ttl_s),
        qr_payload=json.dumps({"sid": str(sid), "n": nonce, "d": body.direction}, separators=(",", ":")),
    )


@router.post("/passages/{session_id}/sign", response_model=SignOut)
def sign_passage(session_id: UUID, body: SignIn):
    """Без токена: телефон подписывает session_id|nonce|direction ключом устройства."""
    # STUB
    return SignOut(status="signed", person=SignedPerson(fio=_FIO))


@router.get("/passages/{session_id}", response_model=PassageStatusOut)
def get_passage(session_id: UUID, user: ControllerDep):
    """Статус сессии; планшет опрашивает раз в секунду."""
    # STUB
    return PassageStatusOut(
        session_id=session_id, status="signed", direction="in",
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
        person=PersonRef(id=1, fio=_FIO), decision=None, reason_code=None, reason=None,
    )


@router.post("/passages/{session_id}/face", response_model=FaceResultOut)
def check_face(
    session_id: UUID,
    user: ControllerDep,
    frames: Annotated[list[UploadFile], File(min_length=3, max_length=3)],
):
    """Три кадра лица (multipart, поле frames); сервер сам решает allow/deny."""
    # STUB
    now = datetime.now(timezone.utc)
    return FaceResultOut(
        decision="allow", reason_code=None, reason=None, person=PersonRef(id=1, fio=_FIO),
        shift=ShiftOut(id=1, started_at=now, ended_at=None, worked_minutes=None),
    )
