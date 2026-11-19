"""Validate the two identifiers naming a Home Assistant service."""

import re

_IDENTIFIER = re.compile(r"[a-z0-9_]+")


class InvalidServiceIdentifier(ValueError):
    def __init__(self, parameter: str) -> None:
        self.parameter = parameter
        super().__init__(
            f"{parameter} must contain only lowercase ASCII letters, digits, or underscores."
        )


def validate_service_identifiers(domain: str, service: str) -> None:
    for parameter, value in (("domain", domain), ("service", service)):
        if not _IDENTIFIER.fullmatch(value):
            raise InvalidServiceIdentifier(parameter)
