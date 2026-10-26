"""Typed service failures without inferring non-execution from their diagnostics."""

from .rest_client import (
    HomeAssistantAdminRequiredError,
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantConnectionError,
)


class HomeAssistantCommandOutcomeUnknown(HomeAssistantConnectionError):
    """A command may have been sent but no usable transport reply arrived."""


class HomeAssistantServiceConnectionOutcomeUnknown(HomeAssistantCommandOutcomeUnknown):
    """A service command's transport failed after it may have reached HA."""


class HomeAssistantServiceAPIOutcomeUnknown(HomeAssistantAPIError):
    """A service HTTP error response that does not establish absence of an effect."""


class HomeAssistantServiceAuthOutcomeUnknown(HomeAssistantAuthError):
    """An auth diagnostic after a service attempt, not proof it had no effect."""

    def __init__(
        self, message: str, *, status_code: int | None, code: object = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_data = {"message": message}
        self.code = code if isinstance(code, str) else None


class HomeAssistantWebSocketServiceAuthError(
    HomeAssistantServiceAuthOutcomeUnknown, HomeAssistantAdminRequiredError
):
    """Keep the existing permission-error catch while retaining an unknown effect."""


class HomeAssistantWebSocketServiceError(HomeAssistantAPIError):
    """A service-adapter failure with an optional authoritative Core error code.

    The inherited 400 preserves existing service-error handling. It is synthetic,
    so write-outcome consumers must use the native code instead of that status.
    """

    def __init__(self, message: str, *, code: object) -> None:
        super().__init__(
            f"API error: 400 - {message}",
            status_code=400,
            response_data={"message": message},
        )
        self.code = code if isinstance(code, str) else None


def is_service_outcome_unknown(error: Exception) -> bool:
    """Recognize local service-attempt types before generic error handling."""
    return isinstance(
        error,
        (
            HomeAssistantServiceAPIOutcomeUnknown,
            HomeAssistantServiceAuthOutcomeUnknown,
            HomeAssistantServiceConnectionOutcomeUnknown,
        ),
    ) or (
        isinstance(error, HomeAssistantWebSocketServiceError)
        and error.code != "unknown_command"
    )
