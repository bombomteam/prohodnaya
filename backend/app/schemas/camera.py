from pydantic import BaseModel


class CameraStatusOut(BaseModel):
    camera_id: str
    online: bool
    fps: float | None
    last_frame_age_ms: float | None
