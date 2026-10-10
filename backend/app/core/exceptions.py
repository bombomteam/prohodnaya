class ApiError(Exception):
    """Бизнес-ошибка API: code для логики фронта, message для человека (по-русски)."""

    def __init__(self, code: str, message: str, http_status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
