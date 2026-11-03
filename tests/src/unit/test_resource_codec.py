"""Unit tests for the inline dashboard-resource codec."""

import base64
import json

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools.resource_codec import (
    _ALLOWED_DATA_URI_PREFIXES,
    LEGACY_WORKER_BASE_URL,
    MAX_CONTENT_SIZE,
    _data_uri_for,
    _decode_data_uri,
    _decode_inline_content,
    _decode_legacy_worker_url,
    _is_data_url,
)


def _legacy_url(content: str, resource_type: str = "module") -> str:
    """Build a legacy worker inline URL the way the retired writer did."""
    encoded = base64.urlsafe_b64encode(content.encode()).decode()
    return f"{LEGACY_WORKER_BASE_URL}/{encoded}?type={resource_type}"


class TestHelperFunctions:
    """Test helper functions."""

    def test_data_uri_roundtrip(self):
        """Content encodes to a data: URI that decodes back byte-identical."""
        content = "export const x = 'ünïcode';"
        url = _data_uri_for(content, "module")

        assert url.startswith("data:text/javascript;base64,")
        assert _decode_data_uri(url) == content

    def test_data_uri_css_roundtrip_with_charset(self):
        """CSS gets an explicit UTF-8 charset and round-trips non-ASCII."""
        content = '.card::before { content: "ünïcode ✓"; }'
        url = _data_uri_for(content, "css")

        assert url.startswith("data:text/css;charset=utf-8;base64,")
        assert _decode_data_uri(url) == content

    def test_data_uri_mime_by_type(self):
        """module → text/javascript, css → text/css with charset."""
        assert _data_uri_for(".x {}", "css").startswith(
            "data:text/css;charset=utf-8;base64,"
        )
        assert _data_uri_for("1;", "module").startswith("data:text/javascript;base64,")

    def test_data_uri_for_rejects_non_inline_type(self):
        """The runtime guard is exercised directly, not only via the caller.

        _set_inline_resource rejects 'js' earlier, so this branch is
        statically unreachable through the tool; the test keeps it honest
        as defence for any future dynamic caller.
        """
        with pytest.raises(ToolError) as exc_info:
            _data_uri_for("x", "js")  # type: ignore[arg-type]

        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "js" in error_data["error"]["message"]

    def test_data_uri_deterministic(self):
        """Same content always maps to the same URL."""
        assert _data_uri_for("const a = 1;", "module") == _data_uri_for(
            "const a = 1;", "module"
        )

    def test_data_uri_roundtrip_at_max_size(self):
        """A payload at exactly MAX_CONTENT_SIZE survives the round-trip."""
        content = "x" * MAX_CONTENT_SIZE
        assert _decode_data_uri(_data_uri_for(content, "module")) == content

    def test_decode_data_uri_rejects_non_data(self):
        """Non-data: URLs decode to None."""
        assert _decode_data_uri("/local/card.js") is None
        assert _decode_data_uri("https://cdn.example.com/card.js") is None

    def test_decode_data_uri_rejects_percent_encoded_form(self):
        """Percent-encoded (non-base64) data: URIs are not claimed as ours."""
        assert _decode_data_uri("data:text/javascript,console.log(1)") is None

    def test_decode_data_uri_rejects_foreign_media_type(self):
        """data: URLs with media types we never write are not claimed."""
        assert _decode_data_uri("data:image/png;base64,aGVsbG8=") is None
        assert _decode_data_uri("data:text/html;base64,aGVsbG8=") is None

    def test_decode_data_uri_rejects_corrupt_payload(self):
        """Non-alphabet bytes in the payload decode to None, not silently
        truncated content — the browser's forgiving-base64 rejects them, so
        reporting 'valid' content would point diagnosis away from the cause."""
        good = _data_uri_for("console.log(1)", "module")
        assert _decode_data_uri(good + "!!junktail") is None

    def test_decode_data_uri_scheme_case_insensitive(self):
        """Scheme/media type match case-insensitively; payload case preserved."""
        content = "const CasePreserved = 1;"
        url = _data_uri_for(content, "module")
        upper_head = url[: len("data:text/javascript;base64,")].upper()
        payload = url[len("data:text/javascript;base64,") :]
        assert _decode_data_uri(upper_head + payload) == content

    def test_decode_inline_content_data_uri(self):
        """Our data: URIs decode as non-legacy inline content."""
        content = "export const x = 1;"
        assert _decode_inline_content(_data_uri_for(content, "module")) == (
            content,
            False,
        )
        assert _decode_inline_content(_data_uri_for(".x {}", "css")) == (".x {}", False)

    def test_decode_inline_content_legacy_worker(self):
        """Legacy worker URLs decode and are flagged as legacy."""
        content = "export const legacy = 1;"
        assert _decode_inline_content(_legacy_url(content)) == (content, True)

    def test_decode_inline_content_non_inline(self):
        """Foreign and undecodable URLs yield no content.

        This is the single source of truth for the list tool's inline_count
        AND its per-resource markers, so anything returning None here must
        also be left unmarked and uncounted.
        """
        for url in (
            "/local/card.js",
            "https://cdn.example.com/card.js",
            "data:image/png;base64,aGVsbG8=",
            "data:text/javascript,console.log(1)",
            "data:text/css;charset=utf-8;base64,",
            "data:text/javascript;base64,!!not-base64!!",
        ):
            assert _decode_inline_content(url) is None, url

    def test_decode_inline_content_lookalike_host(self):
        """Detection is anchored — lookalike hosts embedding the worker host
        string are neither decoded nor flagged."""
        lookalike = LEGACY_WORKER_BASE_URL.replace(
            "https://", "https://evil.example.com/"
        )
        assert _decode_inline_content(f"{lookalike}/YWJj?type=module") is None
        suffixed = f"{LEGACY_WORKER_BASE_URL}.evil.example.com/YWJj"
        assert _decode_inline_content(suffixed) is None

    def test_is_data_url_normalizes_like_a_browser(self):
        """Leading C0/space and embedded tab/newline are stripped before the
        scheme check, matching WHATWG URL parsing — otherwise a padded
        data: URL slips past the url= guard and still loads in a browser."""
        assert _is_data_url("data:text/javascript;base64,YWJj") is True
        assert _is_data_url("DATA:text/javascript;base64,YWJj") is True
        assert _is_data_url(" data:text/javascript,x") is True
        assert _is_data_url("\tdata:text/javascript,x") is True
        assert _is_data_url("\n data:text/javascript,x") is True
        assert _is_data_url("\x00data:text/javascript,x") is True
        assert _is_data_url("da\tta:text/javascript,x") is True
        assert _is_data_url("/local/card.js") is False
        assert _is_data_url("https://cdn.example.com/data:x") is False

    @pytest.mark.parametrize("prefix", _ALLOWED_DATA_URI_PREFIXES)
    def test_decode_data_uri_accepts_every_generated_prefix(self, prefix):
        """All four accepted prefixes decode, including the two read-compat
        forms the writer never emits (js-with-charset, css-without).

        Dropping them as 'dead' would silently stop recognizing resources
        written by a differently-versioned ha-mcp: they would lose _inline,
        fall out of inline_count, and — critically — escape the truncated
        preview guard, so an agent could overwrite them with a preview.
        """
        import base64 as _b64

        content = "export const x = 1;"
        url = prefix + _b64.b64encode(content.encode()).decode()
        assert _decode_data_uri(url) == content

    def test_decode_data_uri_rejects_invalid_utf8(self):
        """Valid base64 that is not valid UTF-8 decodes to None.

        Distinct code path from the corrupt-alphabet case (UnicodeDecodeError
        rather than binascii.Error).
        """
        import base64 as _b64

        payload = _b64.b64encode(bytes([255, 254, 0]) + b"abc").decode()
        assert _decode_data_uri("data:text/javascript;base64," + payload) is None

    def test_decode_legacy_worker_url_rejects_corrupt_payload(self):
        """The legacy decoder is as strict as the data: one.

        urlsafe_b64decode silently DISCARDS non-alphabet characters, so
        without an explicit check a junk path could decode to plausible text
        and be masked as inline content.
        """
        assert (
            _decode_legacy_worker_url(f"{LEGACY_WORKER_BASE_URL}/$$$$aGVsbG8=") is None
        )
        assert (
            _decode_legacy_worker_url(f"{LEGACY_WORKER_BASE_URL}/some-random-path")
            is None
        )

    def test_decode_legacy_worker_url_case_insensitive_origin(self):
        """Scheme/host case does not lose a legacy resource its migration path."""
        content = "export const legacy = 1;"
        url = _legacy_url(content)
        shouty = url.replace("https://", "HTTPS://", 1)
        assert _decode_legacy_worker_url(shouty) == content

    def test_decode_legacy_worker_url(self):
        """Test decoding legacy worker URL (pure local base64)."""
        content = "export const x = 1;"
        assert _decode_legacy_worker_url(_legacy_url(content)) == content

    def test_decode_legacy_worker_url_non_worker(self):
        """Test decoding non-worker URL returns None."""
        assert _decode_legacy_worker_url("/local/card.js") is None


class TestConstants:
    """Test module constants."""

    def test_max_content_size_pinned(self):
        """Pin the cap at 128KB — sized to fit established single-file card
        bundles. The registered URL ships to every browser on each dashboard
        load and auto-backup snapshots it whole per edit, so changing this
        needs a deliberate decision."""
        assert MAX_CONTENT_SIZE == 128_000
