import json
import logging
import sys

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import settings
from app.errors import ApiError


class JsonFormatter(logging.Formatter):
    """Одна JSON-строка на запись лога; трассировка (если есть) идёт в поле exc."""

    def format(self, record: logging.LogRecord) -> str:
        data = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False)


def setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)


setup_logging()
log = logging.getLogger("app")

app = FastAPI(title="Проходная: бэкенд")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _error(status: int, code: str, message: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(status_code=status, content={"code": code, "message": message}, headers=headers)


_HTTP_MESSAGES = {"Not Found": "Не найдено", "Method Not Allowed": "Метод не поддерживается"}


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


@app.get("/health")
def health():
    """Проверка живости процесса; БД намеренно не трогаем."""
    return {"status": "ok"}
