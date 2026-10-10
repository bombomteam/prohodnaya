from fastapi import APIRouter

from app.schemas.locations import LocationsIn, LocationsOut

router = APIRouter(tags=["locations"])


@router.post("/locations", response_model=LocationsOut)
def post_locations(body: LocationsIn):
    """Без токена: пачка геоточек, подписанная ключом устройства."""
    # STUB
    return LocationsOut(accepted=3, rejected=0)
