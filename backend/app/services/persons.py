"""Бизнес-логика людей: заведение и изменение с проверкой эталонного фото."""
import contextlib
import logging
import os
import tempfile
from pathlib import Path

from sqlalchemy import Row
from sqlalchemy.orm import Session

from app import face
from app.core.config import settings
from app.core.exceptions import ApiError
from app.repositories import groups as groups_repo
from app.repositories import persons as repo
from app.schemas.common import GroupRef, ZoneRef
from app.schemas.persons import PersonOut
from app.services import audit

log = logging.getLogger("app.persons")


def _not_found() -> ApiError:
    return ApiError("PERSON_NOT_FOUND", "Человек не найден", 404)


def _to_out(r: Row) -> PersonOut:
    return PersonOut(
        id=r.id, fio=r.fio, position=r.position,
        group=GroupRef(id=r.group_id, name=r.group_name) if r.group_id is not None else None,
        status=r.status, has_face=r.has_face, has_active_device=r.has_active_device,
        on_site=r.on_site_since is not None, on_site_since=r.on_site_since, last_seen_at=r.last_seen_at,
        last_zone=ZoneRef(id=r.zone_id, name=r.zone_name) if r.zone_id is not None else None,
    )


def list_persons(db: Session, q: str | None, group_id: int | None, status: str | None, on_site: bool | None) -> list[PersonOut]:
    return [_to_out(r) for r in repo.list_with_state(db, q, group_id, status, on_site)]


def get_person(db: Session, person_id: int) -> PersonOut:
    rows = repo.list_with_state(db, person_id=person_id)
    if not rows:
        raise _not_found()
    return _to_out(rows[0])


def _check_group(db: Session, group_id: int | None) -> None:
    if group_id is not None and groups_repo.get(db, group_id) is None:
        raise ApiError("GROUP_NOT_FOUND", "Группа не найдена", 422)


_IMAGE_TYPES = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}
_UNAVAILABLE = ("FACE_SERVICE_UNAVAILABLE", "Сервис распознавания недоступен, попробуйте позже", 503)


def _image_ext(photo: bytes) -> str:
    """Тип фото по сигнатуре файла (content_type от клиента не доверяем): jpg, png или webp."""
    if photo.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if photo.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if photo[:4] == b"RIFF" and photo[8:12] == b"WEBP":
        return "webp"
    raise ApiError("FACE_UNSUPPORTED_FORMAT", "Формат не поддерживается. Нужен JPEG, PNG или WEBP.", 422)


def _check_photo(photo: bytes) -> tuple[face.ReferenceCheck, str]:
    """Проверяет эталонное фото через face.check_reference; возвращает ответ модуля и расширение файла.

    Вызывается ДО любых изменений в БД: отказ модуля — 422 с кодом FACE_<REASON> и текстом модуля,
    недоступность модуля или ответ без эмбеддинга — 503. Запись при отказе не создаётся.
    """
    ext = _image_ext(photo)
    try:
        check = face.check_reference(photo, f"reference.{ext}", _IMAGE_TYPES[ext])
    except face.FaceRejected as e:
        raise ApiError("FACE_" + e.reason.code.upper(), e.reason.text, 422)
    except face.FaceUnavailable:
        log.error("Сервис распознавания недоступен при проверке эталонного фото")
        raise ApiError(*_UNAVAILABLE)
    if not check.accepted:
        raise ApiError("FACE_" + check.reason.code.upper(), check.reason.text, 422)
    if check.embedding is None or check.reference_hash is None:  # контракт нарушен: принято, но без эталона
        log.error("face.check_reference принял фото без embedding/reference_hash")
        raise ApiError(*_UNAVAILABLE)
    return check, ext


def _photo_path(person_id: int, ext: str) -> Path:
    return Path(settings.data_dir) / "photos" / f"{person_id}.{ext}"


def _drop(tmp: Path | None) -> None:
    if tmp is not None:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)


def _commit(db: Session, person_id: int, photo: bytes | None, ext: str, old_path: str | None = None) -> None:
    """Коммитит транзакцию вместе с файлом фото.

    Сначала фото пишется во временный файл (упало — откат, в БД ничего), затем commit,
    затем атомарная подмена `<id>.<ext>`: старое фото не теряется, если commit не прошёл.
    Если расширение сменилось (jpg → png), старый файл удаляется после успешной подмены.
    """
    if photo is None:
        db.commit()
        return
    path = _photo_path(person_id, ext)
    tmp: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")  # уникальный: параллельные загрузки не делят файл
        tmp = Path(tmp_name)
        with os.fdopen(fd, "wb") as f:
            f.write(photo)
    except OSError:
        log.error("Не удалось записать фото person_id=%s", person_id, exc_info=True)
        db.rollback()
        _drop(tmp)
        raise ApiError("PHOTO_SAVE_FAILED", "Не удалось сохранить фото, попробуйте ещё раз", 500)
    try:
        db.commit()
    except Exception:
        _drop(tmp)
        raise
    try:
        os.replace(tmp, path)
    except OSError:
        log.exception("Не удалось подменить фото person_id=%s после commit", person_id)
        _drop(tmp)
        raise ApiError("PHOTO_SAVE_FAILED", "Не удалось сохранить фото, попробуйте ещё раз", 500)
    if old_path and Path(old_path) != path:
        _drop(Path(old_path))


def create_person(db: Session, actor_sub: str, fio: str, position: str | None, group_id: int | None, photo: bytes) -> PersonOut:
    """Заводит человека: проверка фото → persons + face_templates + аудит в одной транзакции → файл фото."""
    check, ext = _check_photo(photo)
    _check_group(db, group_id)
    person = repo.add(db, fio, position or None, group_id)
    repo.set_template(db, person.id, check.embedding, check.model_version, check.reference_hash, str(_photo_path(person.id, ext)))
    audit.write(db, actor_sub, "person.create", {"person_id": person.id, "fio": fio, "group_id": group_id})
    _commit(db, person.id, photo, ext)
    log.info("Заведён человек id=%s", person.id)
    return get_person(db, person.id)


def update_person(
    db: Session, actor_sub: str, person_id: int, *, fio: str | None, position: str | None, group_id: int | None,
    status: str | None, photo: bytes | None, cleared: set[str],
) -> PersonOut:
    """Частичное изменение. None = поле не передано; имя из `cleared` (пустая строка в форме) = обнулить.

    Новое фото проверяется как при заведении и заменяет эталон и файл.
    """
    person = repo.get(db, person_id)
    if person is None:
        raise _not_found()
    check, ext = _check_photo(photo) if photo is not None else (None, "")
    new: dict[str, object] = {}
    if fio is not None:
        new["fio"] = fio
    if "position" in cleared or position is not None:
        new["position"] = None if "position" in cleared else position
    if "group_id" in cleared or group_id is not None:
        new["group_id"] = None if "group_id" in cleared else group_id
        _check_group(db, new["group_id"])
    if status is not None:
        new["status"] = status
    changed = [k for k, v in new.items() if getattr(person, k) != v]
    for k in changed:
        setattr(person, k, new[k])
    old_template = repo.get_template(db, person_id)
    old_path = old_template.photo_path if old_template else None
    if check is not None:
        repo.set_template(db, person_id, check.embedding, check.model_version, check.reference_hash, str(_photo_path(person_id, ext)))
        changed.append("photo")
    if changed:  # без реальных изменений аудит не пишем
        audit.write(db, actor_sub, "person.update", {"person_id": person_id, "fields": changed})
    _commit(db, person_id, photo, ext, old_path)
    log.info("Изменён человек id=%s поля=%s", person_id, ",".join(changed))
    return get_person(db, person_id)


def photo_file(db: Session, person_id: int) -> tuple[Path, str]:
    """Путь и media_type эталонного фото; 404, если человека, эталона или файла нет."""
    if repo.get(db, person_id) is None:
        raise _not_found()
    template = repo.get_template(db, person_id)
    if template is None or not Path(template.photo_path).is_file():
        raise ApiError("PHOTO_NOT_FOUND", "Фото не найдено", 404)
    path = Path(template.photo_path)
    return path, _IMAGE_TYPES.get(path.suffix.lstrip("."), "application/octet-stream")
