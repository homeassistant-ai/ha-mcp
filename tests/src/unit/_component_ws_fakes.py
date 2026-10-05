"""Shared test doubles for the component WebSocket command surface.

Extracted from ``test_component_ws_search.py`` and
``test_component_ws_phase2_async.py``: both are over the module size ratchet's
limit and cannot grow, and these doubles were already shared across five test
modules. Those two modules re-export the names they previously defined, so
existing ``from .test_component_ws_search import _FakeConnection`` imports keep
working.

``_FakeWSApi`` is a FUNCTIONAL stand-in for
``homeassistant.components.websocket_api`` — its decorators really wrap, so a
handler registered through it exercises the ``@require_admin`` /
``@async_response`` stack instead of bypassing it. ``_FakeConnection`` mirrors
the parts of core's ``ActiveConnection`` the component touches: ``user``,
``context()`` and ``send_result()``.

Nothing here may import ``homeassistant`` — ``test_component_ws_phase2_async``
imports this module before the ``homeassistant.*`` MagicMock stubs are installed.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Collection, Mapping
from typing import Any


class _Unauthorized(Exception):
    pass


class _FakeUser:
    def __init__(self, is_admin: bool, user_id: str | None = None) -> None:
        self.is_admin = is_admin
        self.id = user_id


class _FakeContext:
    """What ``_FakeConnection.context()`` returns, mirroring ``homeassistant.core.Context``.

    Only ``user_id`` is modelled: that is the field the component's write path
    forwards and the one the logbook reads to attribute a change.
    """

    def __init__(self, user_id: str | None) -> None:
        self.user_id = user_id


class _FakeConnection:
    def __init__(
        self, is_admin: bool = True, has_user: bool = True, user_id: str | None = None
    ) -> None:
        self.user = _FakeUser(is_admin, user_id) if has_user else None
        self.results: dict[Any, Any] = {}

    def context(self, msg: dict[str, Any]) -> _FakeContext:
        """Mirror ``ActiveConnection.context``, which returns ``Context(user_id=user.id)``."""
        return _FakeContext(self.user.id if self.user is not None else None)

    def send_result(self, msg_id: Any, result: Any) -> None:
        self.results[msg_id] = result


class _FakeWSApi:
    """Functional stand-in for homeassistant.components.websocket_api."""

    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}

    def websocket_command(self, schema: dict[Any, Any]) -> Any:
        command = next(v for k, v in schema.items() if str(k) == "type")

        def decorate(func: Any) -> Any:
            func._ws_command = command
            func._ws_schema = schema
            return func

        return decorate

    def require_admin(self, func: Any) -> Any:
        @functools.wraps(func)
        def wrapper(hass: Any, connection: Any, msg: dict[str, Any]) -> Any:
            user = connection.user
            if user is None or not user.is_admin:
                raise _Unauthorized()
            return func(hass, connection, msg)

        return wrapper

    def async_response(self, func: Any) -> Any:
        @functools.wraps(func)
        def wrapper(hass: Any, connection: Any, msg: dict[str, Any]) -> None:
            # The handler is a coroutine (it awaits the search prep's executor
            # offload); drive it to completion the way the WS layer would.
            asyncio.run(func(hass, connection, msg))

        return wrapper

    def async_register_command(self, hass: Any, handler: Any) -> None:
        self.registered[handler._ws_command] = handler


class _FakeCallServices:
    """``hass.services`` stand-in with the write surface ``call_service`` drives.

    ``has_service`` answers from a known ``{(domain, service)}`` set; ``async_call``
    records its args, optionally runs an ``on_call`` hook (used to fire the
    confirming state_changed event mid-dispatch), optionally raises, else returns a
    canned ``response``.
    """

    def __init__(
        self,
        *,
        known: Collection[tuple[str, str]] = (),
        response: Any = None,
        on_call: Any = None,
        raises: BaseException | None = None,
    ) -> None:
        self._known = set(known)
        self._response = response
        self._on_call = on_call
        self._raises = raises
        self.calls: list[dict[str, Any]] = []

    def has_service(self, domain: str, service: str) -> bool:
        return (domain, service) in self._known

    async def async_call(
        self,
        domain: str,
        service: str,
        service_data: dict[str, Any] | None,
        blocking: bool = True,
        return_response: bool = False,
        context: Any = None,
    ) -> Any:
        self.calls.append(
            {
                "domain": domain,
                "service": service,
                "service_data": service_data,
                "blocking": blocking,
                "return_response": return_response,
                "context": context,
            }
        )
        if self._raises is not None:
            raise self._raises
        if self._on_call is not None:
            self._on_call()
        return self._response

    @property
    def call_count(self) -> int:
        return len(self.calls)


class _FakeBulkServices:
    """``hass.services`` stand-in with PER-``(domain, service)`` dispatch behavior.

    ``behaviors`` maps ``(domain, service)`` to an optional dict carrying ``on_call``
    (a hook run mid-dispatch, e.g. fire THIS op's confirming state_changed event),
    ``raises`` (an exception to raise for THIS op only), and ``response``. Unlike the
    single-call ``_FakeCallServices`` (one shared hook / raise), the per-op routing
    lets one batch op raise while another confirms — the parallel-isolation case.
    ``has_service`` answers from the behavior keys plus any extra ``known``.
    """

    def __init__(
        self,
        behaviors: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
        known: Collection[tuple[str, str]] = (),
    ) -> None:
        self._behaviors = {k: dict(v) for k, v in dict(behaviors or {}).items()}
        self._known = set(known) | set(self._behaviors)
        self.calls: list[dict[str, Any]] = []

    def has_service(self, domain: str, service: str) -> bool:
        return (domain, service) in self._known

    async def async_call(
        self,
        domain: str,
        service: str,
        service_data: dict[str, Any] | None,
        blocking: bool = True,
        return_response: bool = False,
        context: Any = None,
    ) -> Any:
        self.calls.append(
            {
                "domain": domain,
                "service": service,
                "service_data": service_data,
                "blocking": blocking,
                "return_response": return_response,
                "context": context,
            }
        )
        behavior = self._behaviors.get((domain, service), {})
        if behavior.get("raises") is not None:
            raise behavior["raises"]
        if behavior.get("on_call") is not None:
            behavior["on_call"]()
        return behavior.get("response")

    @property
    def call_count(self) -> int:
        return len(self.calls)
