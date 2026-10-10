import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name) or default)


@dataclass(frozen=True)
class Settings:
    database_url: str
    data_dir: str
    cors_origins: list[str]
    public_app_url: str
    site_tz: str
    pass_session_ttl_s: int
    pass_code_max_attempts: int
    pass_code_request_max_skew_s: int
    enroll_code_ttl_min: int
    face_url: str
    face_timeout_s: float
    face_camera_id: str
    location_interval_s: int
    location_max_age_s: int
    auth_dev_bypass: bool
    kc_public_url: str
    kc_internal_url: str
    auth_redirect_uris: list[str]
    llm_provider: str
    llm_external_url: str
    llm_external_model: str
    llm_api_key: str
    ollama_url: str
    ollama_model: str
    llm_timeout_s: float


def load_settings() -> Settings:
    """Читает все настройки из переменных окружения; пустое значение = значение по умолчанию."""
    env = os.environ.get
    public_app_url = env("PUBLIC_APP_URL", "")
    redirect_uris = [u.strip() for u in (env("AUTH_REDIRECT_URIS") or "").split(",") if u.strip()]
    if not redirect_uris and public_app_url:
        redirect_uris = [public_app_url.rstrip("/") + "/"]  # фронт всегда шлёт location.origin + "/"
    return Settings(
        database_url=env("DATABASE_URL", ""),
        data_dir=env("DATA_DIR") or "/data",
        cors_origins=[o.strip() for o in (env("CORS_ORIGINS") or "").split(",") if o.strip()],
        public_app_url=public_app_url,
        site_tz=env("SITE_TZ") or "Europe/Moscow",
        pass_session_ttl_s=_int("PASS_SESSION_TTL_S", 90),
        pass_code_max_attempts=_int("PASS_CODE_MAX_ATTEMPTS", 5),
        pass_code_request_max_skew_s=_int("PASS_CODE_REQUEST_MAX_SKEW_S", 30),
        enroll_code_ttl_min=_int("ENROLL_CODE_TTL_MIN", 60),
        face_url=(env("FACE_URL") or "http://face:8000").rstrip("/"),
        face_timeout_s=float(env("FACE_TIMEOUT_S") or 5),
        face_camera_id=env("FACE_CAMERA_ID") or "gate-1",
        location_interval_s=_int("LOCATION_INTERVAL_S", 30),
        location_max_age_s=_int("LOCATION_MAX_AGE_S", 180),
        auth_dev_bypass=(env("AUTH_DEV_BYPASS") or "false").lower() == "true",
        kc_public_url=env("KC_PUBLIC_URL", ""),
        kc_internal_url=env("KC_INTERNAL_URL", ""),
        auth_redirect_uris=redirect_uris,
        llm_provider=(env("LLM_PROVIDER") or "ollama").lower(),
        llm_external_url=env("LLM_EXTERNAL_URL") or "",
        llm_external_model=env("LLM_EXTERNAL_MODEL") or "",
        llm_api_key=env("LLM_API_KEY") or "",
        ollama_url=(env("OLLAMA_URL") or "http://ollama:11434").rstrip("/"),
        ollama_model=env("OLLAMA_MODEL") or "qwen2.5:1.5b",
        llm_timeout_s=float(env("LLM_TIMEOUT_S") or 60),
    )


settings = load_settings()
