"""Pytest configuration helpers for the test suite."""


def pytest_addoption(parser):
    """Register ini options used by the repository when optional plugins are absent."""

    parser.addini(
        "asyncio_mode",
        "Compatibility shim for environments without pytest-asyncio",
        default="auto",
    )
