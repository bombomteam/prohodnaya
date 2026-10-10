from fastapi import APIRouter

from app.dependency import DbDep
from app.schemas.locations import LocationsIn, LocationsOut
from app.services import locations as service

router = APIRouter(tags=["locations"])


@router.post("/locations", response_model=LocationsOut)
def post_locations(body: LocationsIn, db: DbDep):
    """Без токена: пачка геоточек, подписанная ключом устройства."""
    return service.accept(db, body)
