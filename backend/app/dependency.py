"""Depends-обёртки: роуты берут пользователя, проверку роли и сессию БД через эти алиасы."""
from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from app.core.auth import current_user, require_role
from app.core.database import get_db

CurrentUserDep = Annotated[dict, Depends(current_user)]
AdminDep = Annotated[dict, Depends(require_role("admin"))]
ManagerDep = Annotated[dict, Depends(require_role("manager"))]
AdminOrManagerDep = Annotated[dict, Depends(require_role("admin", "manager"))]
ControllerDep = Annotated[dict, Depends(require_role("controller"))]
DbDep = Annotated[Session, Depends(get_db)]
