from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, File, Form, Query, Response, UploadFile

from app.dependency import AdminDep
from app.schemas.common import GroupRef, ZoneRef
from app.schemas.persons import PersonOut, PersonStatus

router = APIRouter(tags=["persons"])

_JPEG_STUB = b"\xff\xd8\xff\xd9"


def _example_person() -> PersonOut:
    now = datetime.now(timezone.utc)
    return PersonOut(
        id=1, fio="Иванов Иван Иванович", position="Монтажник", group=GroupRef(id=1, name="Бригада А"),
        status="active", has_face=True, has_active_device=True, on_site=True, on_site_since=now,
        last_seen_at=now, last_zone=ZoneRef(id=1, name="Склад"),
    )


@router.post("/persons", response_model=PersonOut)
def create_person(
    user: AdminDep,
    fio: Annotated[str, Form(min_length=1)],
    photo: Annotated[UploadFile, File()],
    position: Annotated[str | None, Form()] = None,
    group_id: Annotated[int | None, Form()] = None,
):
    """Заводит человека с эталонным фото (multipart)."""
    # STUB
    return _example_person()


@router.get("/persons", response_model=list[PersonOut])
def list_persons(
    user: AdminDep,
    q: Annotated[str | None, Query()] = None,
    group_id: Annotated[int | None, Query()] = None,
    status: Annotated[PersonStatus | None, Query()] = None,
    on_site: Annotated[bool | None, Query()] = None,
):
    """Список людей с текущим состоянием."""
    # STUB
    return [_example_person()]


@router.get("/persons/{person_id}", response_model=PersonOut)
def get_person(person_id: int, user: AdminDep):
    """Один человек с текущим состоянием."""
    # STUB
    return _example_person()


@router.patch("/persons/{person_id}", response_model=PersonOut)
def update_person(
    person_id: int,
    user: AdminDep,
    fio: Annotated[str | None, Form(min_length=1)] = None,
    position: Annotated[str | None, Form()] = None,
    group_id: Annotated[int | None, Form()] = None,
    status: Annotated[PersonStatus | None, Form()] = None,
    photo: Annotated[UploadFile | None, File()] = None,
):
    """Меняет данные человека (multipart, все поля необязательны); новое фото заменяет эталон."""
    # STUB
    return _example_person()


@router.get("/persons/{person_id}/photo", response_class=Response, responses={200: {"content": {"image/jpeg": {}}}})
def get_person_photo(person_id: int, user: AdminDep):
    """Эталонное фото человека; только для admin."""
    # STUB
    return Response(content=_JPEG_STUB, media_type="image/jpeg")
