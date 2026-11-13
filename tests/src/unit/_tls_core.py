"""Core's own HTTP app served over TLS with a self-signed certificate.

What ``HA_VERIFY_SSL`` decides is whether the server's clients accept such a
certificate; serving the real ``api`` and ``websocket_api`` behind one lets a
test watch them accept it or refuse it, instead of reading what they would
have handed the TLS layer.
"""

from __future__ import annotations

import asyncio
import datetime
import ipaddress
import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp.test_utils import TestServer
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from homeassistant.setup import async_setup_component

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_REFUSED_HANDSHAKE = "Error on transport creation for incoming connection"

# A fixed validity window, so no answer here depends on the wall clock.
_VALID_FROM = datetime.datetime(2000, 1, 1, tzinfo=datetime.UTC)
_VALID_UNTIL = datetime.datetime(2100, 1, 1, tzinfo=datetime.UTC)


def _server_context(directory: Path) -> ssl.SSLContext:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ha-mcp self-signed")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_VALID_FROM)
        .not_valid_after(_VALID_UNTIL)
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = directory / "cert.pem"
    key_path = directory / "key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(cert_path, key_path)
    return context


@asynccontextmanager
async def tls_core(hass: HomeAssistant, directory: Path) -> AsyncIterator[str]:
    """Serve Core's ``api`` and ``websocket_api`` over TLS; yield its base URL.

    The certificate names 127.0.0.1 but no authority signed it, as with a
    self-signed Home Assistant install. The test requests ``socket_enabled``.

    A client that refuses the certificate aborts the handshake, which the
    server side of the loop reports in debug mode as an error creating the
    transport. That report is the refusal seen from the server, so only it is
    kept away from the test's loop exception handler.
    """
    assert await async_setup_component(hass, "api", {})
    assert await async_setup_component(hass, "websocket_api", {})
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()

    def handler(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        if context.get("message") == _REFUSED_HANDSHAKE:
            return
        if previous is None:
            loop.default_exception_handler(context)
        else:
            previous(loop, context)

    loop.set_exception_handler(handler)
    server = TestServer(hass.http.app)
    try:
        await server.start_server(ssl=_server_context(directory))
        yield str(server.make_url("")).rstrip("/")
    finally:
        try:
            await server.close()
        finally:
            loop.set_exception_handler(previous)
