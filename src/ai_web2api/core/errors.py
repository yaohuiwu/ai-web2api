"""统一错误类型 → HTTP 状态码映射。"""

from __future__ import annotations


class ProviderError(Exception):
    """Provider 层错误基类，由 FastAPI 异常处理器映射为 JSON 错误响应。"""

    status_code = 500
    error_type = "provider_error"

    def __init__(self, message: str, *, provider: str | None = None, retryable: bool = False):
        super().__init__(message)
        self.message = message
        self.provider = provider
        self.retryable = retryable


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


class ThreadMismatchError(ProviderError):
    """同一 thread_id 请求了不同的 provider/model。"""

    status_code = 409
    error_type = "thread_mismatch"


class UnsupportedModeError(ProviderError):
    """请求了 provider 不支持 / 页面找不到的 mode（客户端参数错误）。"""

    status_code = 400
    error_type = "unsupported_mode"


class AttachmentError(ProviderError):
    """附件不支持/超限（客户端参数错误）。"""

    status_code = 400
    error_type = "attachments_error"


class ThreadExpiredError(ProviderError):
    """thread 页面失效（登出/崩溃/超长会话），已自动销毁，可重试重建。"""

    status_code = 409
    error_type = "thread_expired"


class ThreadTimeoutError(ProviderError):
    """thread 请求超时（页面挂起/上一请求未释放），会话已销毁，可重试重建。"""

    status_code = 504
    error_type = "thread_timeout"


class ThreadBusyError(ProviderError):
    """thread 页面正在生成其他内容（上一请求未完成，排队等待中），会话已销毁，可重试重建。"""

    status_code = 409
    error_type = "thread_busy"
