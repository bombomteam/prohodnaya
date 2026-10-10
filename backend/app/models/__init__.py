"""Импорт всех моделей: Base.metadata должна быть полной (Alembic, тесты)."""
from app.models.audit import AuditLog
from app.models.device import Device, EnrollCode
from app.models.passage import AccessEvent, PassSession, Shift
from app.models.person import FaceTemplate, Group, Person
from app.models.site import LocationPoint, SitePlan, Zone

__all__ = [
    "AccessEvent", "AuditLog", "Device", "EnrollCode", "FaceTemplate", "Group",
    "LocationPoint", "PassSession", "Person", "Shift", "SitePlan", "Zone",
]
