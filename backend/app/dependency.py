"""Depends-обёртки: роуты берут пользователя и проверку роли через эти алиасы."""
from typing import Annotated

from fastapi import Depends

from app.core.auth import current_user, require_role

CurrentUserDep = Annotated[dict, Depends(current_user)]
AdminDep = Annotated[dict, Depends(require_role("admin"))]
ManagerDep = Annotated[dict, Depends(require_role("manager"))]
AdminOrManagerDep = Annotated[dict, Depends(require_role("admin", "manager"))]
ControllerDep = Annotated[dict, Depends(require_role("controller"))]
