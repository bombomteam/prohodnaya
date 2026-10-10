import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.exceptions import ApiError

log = logging.getLogger("app")

_HTTP_MESSAGES = {"Not Found": "Не найдено", "Method Not Allowed": "Метод не поддерживается"}


def _error(status: int, code: str, message: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(status_code=status, content={"code": code, "message": message}, headers=headers)


def register_exception_handlers(app: FastAPI) -> None:
    """Подключает обработчики: все ошибки отдаются как {code, message} с русским текстом."""

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError):
        return _error(exc.http_status, exc.code, exc.message)

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException):
        """Приводит стандартные ошибки HTTP к формату {code, message} с русским текстом."""
        if isinstance(exc.detail, str):
            message = _HTTP_MESSAGES.get(exc.detail, exc.detail)
        else:
            message = "Ошибка запроса"
        return _error(exc.status_code, f"HTTP_{exc.status_code}", message, getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        """Называет поле первой ошибки: последний строковый элемент loc без служебных частей."""
        errors = exc.errors()
        loc = errors[0]["loc"] if errors else ()
        names = [p for p in loc if isinstance(p, str) and p not in ("body", "query", "path", "header", "cookie")]
        if not names:
            return _error(422, "VALIDATION", "Некорректные данные запроса")
        return _error(422, "VALIDATION", f"Некорректные данные: {names[-1]}")

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception):
        log.error("Необработанная ошибка %s %s", request.method, request.url.path, exc_info=exc)
        return _error(500, "INTERNAL", "Внутренняя ошибка сервера, попробуйте ещё раз")
