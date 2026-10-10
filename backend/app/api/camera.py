from fastapi import APIRouter, Response

from app.dependency import CameraViewerDep
from app.schemas.camera import CameraStatusOut
from app.services import camera as service

router = APIRouter(tags=["camera"])


@router.get("/camera/status", response_model=CameraStatusOut)
def camera_status(user: CameraViewerDep):
    """Состояние камеры на входе; модуль недоступен — online=false, не ошибка."""
    return service.get_status()


@router.get(
    "/camera/frame.jpg",
    response_class=Response,
    responses={200: {"content": {"image/jpeg": {}}}, 503: {"description": "CAMERA_OFFLINE"}},
)
def camera_frame(user: CameraViewerDep):
    """Последний кадр камеры с рамками лиц и людей (JPEG, без кеширования)."""
    return Response(service.get_frame_jpeg(), media_type="image/jpeg", headers={"Cache-Control": "no-store"})
