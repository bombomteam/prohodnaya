from fastapi import APIRouter

from app.dependency import DbDep
from app.schemas.passages import PassCodeIn, PassCodeOut
from app.services import passage as service

router = APIRouter(tags=["device"])


@router.post("/device/pass-code", response_model=PassCodeOut, response_model_exclude_none=True)
def pass_code(body: PassCodeIn, db: DbDep):
    """Без токена: телефон подписывает pass-code|device_id|ts и забирает код ждущего прохода (опрос раз в 2–3 с)."""
    return service.pass_code(db, body)
