"""统一错误类型 → HTTP 状态码映射。"""

from __future__ import annotations


class ProviderError(Exception):
    """Provider 层错误基类，由 FastAPI 异常处理器映射为 JSON 错误响应。"""

    status_code = 500
    error_type = "provider_error"

    def __init__(self, message: str, *, provider: str | None = None):
        super().__init__(message)
        self.message = message
        self.provider = provider


class ModelNotFoundError(ProviderError):
    status_code = 404
    error_type = "model_not_found"


class NotLoggedInError(ProviderError):
    status_code = 503
    error_type = "not_logged_in"


class ResponseTimeoutError(ProviderError):
    status_code = 504
    error_type = "response_timeout"


class RateLimitedError(ProviderError):
    status_code = 429
    error_type = "rate_limited"


class QueueFullError(ProviderError):
    status_code = 429
    error_type = "queue_full"
