"""Keep a service call's identifiers intact when building its REST path."""

from urllib.parse import quote


def service_identifier_errors(
    domain: str | None, service: str | None, *, require_exact_case: bool
) -> dict[str, str]:
    errors = {}
    for parameter, value in (("domain", domain), ("service", service)):
        if not value:
            errors[parameter] = "is required"
        elif (
            value in {".", ".."}
            or any(char in "/\\%" for char in value)
            or not value.isprintable()
        ):
            errors[parameter] = "must identify one service path segment"
        elif require_exact_case and value != value.lower():
            # Core lowercases aliases; ha_call_service policy rules see the supplied case.
            errors[parameter] = (
                "must use the lowercase spelling registered in Home Assistant"
            )
    return errors


def service_rest_path(domain: str, service: str) -> str:
    errors = service_identifier_errors(domain, service, require_exact_case=False)
    if errors:
        raise ValueError(", ".join(f"{key} {reason}" for key, reason in errors.items()))
    return f"/services/{quote(domain, safe='')}/{quote(service, safe='')}"
