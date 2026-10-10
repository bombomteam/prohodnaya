from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse

from app.core.exceptions import ApiError
from app.dependency import AdminDep, DbDep
from app.schemas.persons import PersonOut, PersonStatus
from app.services import persons as service

router = APIRouter(tags=["persons"])

MAX_PHOTO_BYTES = 10 * 1024 * 1024
GROUP_ID_MAX = 2**31 - 1  # int4 в БД


def _read_photo(photo: UploadFile) -> bytes:
    """Читает не больше лимита (+1 байт для проверки); больше — 413."""
    data = photo.file.read(MAX_PHOTO_BYTES + 1)
    if len(data) > MAX_PHOTO_BYTES:
        raise ApiError("PHOTO_TOO_LARGE", "Фото слишком большое, максимум 10 МБ", 413)
    return data


async def _cleared_fields(request: Request) -> set[str]:
    """Поля формы, присланные пустой строкой: FastAPI считает их «не переданными», а для PATCH это «обнулить»."""
    form = await request.form()
    return {k for k, v in form.items() if v == ""}


@router.post("/persons", response_model=PersonOut)
def create_person(
    db: DbDep,
    user: AdminDep,
    fio: Annotated[str, Form(min_length=1)],
    photo: Annotated[UploadFile, File()],
    position: Annotated[str | None, Form()] = None,
    group_id: Annotated[int | None, Form(ge=1, le=GROUP_ID_MAX)] = None,
):
    """Заводит человека с эталонным фото (multipart)."""
    return service.create_person(db, user["sub"], fio, position, group_id, _read_photo(photo))


@router.get("/persons", response_model=list[PersonOut])
def list_persons(
    db: DbDep,
    user: AdminDep,
    q: Annotated[str | None, Query()] = None,
    group_id: Annotated[int | None, Query(ge=1, le=GROUP_ID_MAX)] = None,
    status: Annotated[PersonStatus | None, Query()] = None,
    on_site: Annotated[bool | None, Query()] = None,
):
    """Список людей с текущим состоянием."""
    return service.list_persons(db, q, group_id, status, on_site)


@router.get("/persons/{person_id}", response_model=PersonOut)
def get_person(person_id: int, db: DbDep, user: AdminDep):
    """Один человек с текущим состоянием."""
    return service.get_person(db, person_id)


@router.patch("/persons/{person_id}", response_model=PersonOut)
def update_person(
    person_id: int,
    db: DbDep,
    user: AdminDep,
    cleared: Annotated[set[str], Depends(_cleared_fields)],
    fio: Annotated[str | None, Form(min_length=1)] = None,
    position: Annotated[str | None, Form()] = None,
    group_id: Annotated[str | None, Form(description="id группы; пустая строка — убрать из группы")] = None,
    status: Annotated[PersonStatus | None, Form()] = None,
    photo: Annotated[UploadFile | None, File()] = None,
):
    """Меняет данные человека (multipart, все поля необязательны); пустая строка в position/group_id обнуляет поле."""
    data = _read_photo(photo) if photo is not None else None
    # str, а не int: разные версии FastAPI по-разному читают "" у int-поля формы
    if group_id and not (group_id.isdecimal() and len(group_id) <= 10 and 1 <= int(group_id) <= GROUP_ID_MAX):
        raise ApiError("VALIDATION", "Некорректные данные: group_id", 422)
    return service.update_person(
        db, user["sub"], person_id, fio=fio, position=position, group_id=int(group_id) if group_id else None, status=status,
        photo=data, cleared=cleared,
    )


@router.get("/persons/{person_id}/photo", response_class=FileResponse, responses={200: {"content": {"image/jpeg": {}}}})
def get_person_photo(person_id: int, db: DbDep, user: AdminDep):
    """Эталонное фото человека; только для admin."""
    return FileResponse(service.photo_file(db, person_id), media_type="image/jpeg")
