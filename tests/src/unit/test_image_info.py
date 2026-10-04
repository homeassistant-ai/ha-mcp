"""Unit tests for ha_mcp.image_info (header-only image format/size resolution).

Fixtures are hand-built byte literals — the resolver only reads headers,
so no image encoder is needed and the tests stay dependency-free. The JPEG
fixtures carry an APP1 "Exif" segment before the SOF by default, to
exercise the segment walker against a realistic camera payload; the plain
baseline test opts out with ``app1=False``.

The resolver returns a (format, dimensions) pair: the format and the size
must describe the same bytes, so a mislabeled Content-Type yields the
sniffed format, not the header's.
"""

import random

from ha_mcp.image_info import resolve_image_info


def _jpeg(
    width: int, height: int, extra_segments: bytes = b"", app1: bool = True
) -> bytes:
    """Build a minimal structurally-valid JPEG with the given dimensions.

    Segment length fields include their own two bytes (the JPEG spec's
    ``Lf``), matching what real camera firmware emits.
    """
    app1_payload = b"Exif\x00\x00" + b"\x00" * 16
    app1 = (
        b"\xff\xe1" + (len(app1_payload) + 2).to_bytes(2, "big") + app1_payload
        if app1
        else b""  # no APP1: bare SOI + SOF0 + EOI
    )
    sof0 = (
        b"\xff\xc0"
        + (11).to_bytes(2, "big")
        + b"\x08"  # bit depth
        # The SOF frame header lists Yt (height) before Xs (width), so the
        # wire order is the reverse of the (width, height) argument order.
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + b"\x01"  # one component
        + b"\x01\x11\x00"  # component: id 1, 1x1 sampling, table 0
    )
    return b"\xff\xd8" + extra_segments + app1 + sof0 + b"\xff\xd9"


def _png(width: int, height: int) -> bytes:
    """Build a minimal structurally-valid PNG with the given dimensions."""
    ihdr_payload = (
        width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x06\x00\x00\x00"  # 8-bit RGBA, no interlace
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + len(ihdr_payload).to_bytes(4, "big")
        + b"IHDR"
        + ihdr_payload
        + b"\x00\x00\x00\x00IEND\xaeB\x60\x82"
    )


def _gif(width: int, height: int, version: bytes = b"89a") -> bytes:
    """Build a minimal GIF header (logical screen size) with the size."""
    return b"GIF" + version + width.to_bytes(2, "little") + height.to_bytes(2, "little")


class TestJpeg:
    def test_baseline_jpeg_dimensions(self) -> None:
        # The no-APP1 baseline: SOI, SOF0, EOI and nothing in between.
        assert resolve_image_info(_jpeg(800, 600, app1=False), "jpeg") == (
            "jpeg",
            (800, 600),
        )

    def test_walks_past_exif_app1_segment(self) -> None:
        # A realistic camera JPEG: APP1 "Exif" then SOF0.
        jpeg = _jpeg(1440, 720)
        assert resolve_image_info(jpeg, "jpeg") == ("jpeg", (1440, 720))

    def test_walks_past_restart_markers(self) -> None:
        jpeg = _jpeg(200, 100, extra_segments=b"\xff\xd0" + b"\xff\xd1")
        assert resolve_image_info(jpeg, "jpeg") == ("jpeg", (200, 100))

    def test_fill_bytes_before_sof_are_skipped(self) -> None:
        """T.81 B.1.1.2 allows any run of 0xFF fill bytes before a marker."""
        sof0 = (
            b"\xff\xc0"
            + (11).to_bytes(2, "big")
            + b"\x08"  # bit depth
            + (600).to_bytes(2, "big")
            + (800).to_bytes(2, "big")
            + b"\x01"  # one component
            + b"\x01\x11\x00"  # component: id 1, 1x1 sampling, table 0
        )
        jpeg = b"\xff\xd8" + b"\xff" * 3 + sof0
        assert resolve_image_info(jpeg, "jpeg") == ("jpeg", (800, 600))

    def test_dht_segment_before_sof_is_skipped(self) -> None:
        """A DHT (Huffman table) segment before the SOF must be skipped.

        Pins the explicit SOF marker set against a future simplification to
        ``0xC0 <= marker <= 0xCF``: that range also covers DHT (0xC4), JPG
        (0xC8) and DAC (0xCC), and would stop the walk at the DHT header,
        whose short length field yields no dimensions.
        """
        dht = b"\xff\xc4" + (5).to_bytes(2, "big") + b"\x00\x01\x00"
        sof0 = (
            b"\xff\xc0"
            + (11).to_bytes(2, "big")
            + b"\x08"  # bit depth
            + (600).to_bytes(2, "big")
            + (800).to_bytes(2, "big")
            + b"\x01"  # one component
            + b"\x01\x11\x00"  # component: id 1, 1x1 sampling, table 0
        )
        jpeg = b"\xff\xd8" + dht + sof0
        assert resolve_image_info(jpeg, "jpeg") == ("jpeg", (800, 600))

    def test_progressive_sof2_dimensions(self) -> None:
        """Progressive JPEGs carry the frame in SOF2 (0xC2), not SOF0."""
        sof2 = (
            b"\xff\xc2"
            + (11).to_bytes(2, "big")
            + b"\x08"  # bit depth
            + (600).to_bytes(2, "big")
            + (800).to_bytes(2, "big")
            + b"\x01"  # one component
            + b"\x01\x11\x00"  # component: id 1, 1x1 sampling, table 0
        )
        jpeg = b"\xff\xd8" + sof2 + b"\xff\xd9"
        assert resolve_image_info(jpeg, "jpeg") == ("jpeg", (800, 600))

    def test_no_sof_marker_returns_no_dimensions(self) -> None:
        jpeg = b"\xff\xd8" + b"\xff\xe1" + (6).to_bytes(2, "big") + b"Exif"
        assert resolve_image_info(jpeg, "jpeg") == ("jpeg", None)

    def test_truncated_sof_returns_no_dimensions(self) -> None:
        # SOF marker + declared length present, payload cut off.
        jpeg = b"\xff\xd8" + b"\xff\xc0" + (9).to_bytes(2, "big") + b"\x08\x02"
        assert resolve_image_info(jpeg, "jpeg") == ("jpeg", None)

    def test_sof_length_shorter_than_dimension_fields_returns_none(self) -> None:
        """A corrupt Lf that does not cover the fields must not be trusted.

        The buffer keeps holding bytes after the declared segment, so a
        file-level bounds check alone would read height/width from bytes
        outside the segment (and report them as the image size).
        """
        jpeg = (
            b"\xff\xd8"
            + b"\xff\xc0"
            + (3).to_bytes(2, "big")  # declares 1 payload byte; fields need 5
            + b"\x08"
            + (600).to_bytes(2, "big")
            + (800).to_bytes(2, "big")
            + b"\xff\xd9"
        )
        assert resolve_image_info(jpeg, "jpeg") == ("jpeg", None)

    def test_sof_length_exceeding_buffer_returns_none(self) -> None:
        """A declared segment that runs past the end of the buffer is corrupt."""
        jpeg = (
            b"\xff\xd8"
            + b"\xff\xc0"
            + (30).to_bytes(2, "big")
            + b"\x08"
            + (600).to_bytes(2, "big")
            + (800).to_bytes(2, "big")
        )
        assert resolve_image_info(jpeg, "jpeg") == ("jpeg", None)

    def test_non_jpeg_bytes_return_no_dimensions(self) -> None:
        assert resolve_image_info(b"not an image at all", "jpeg") == ("jpeg", None)

    def test_zero_dimension_returns_no_dimensions(self) -> None:
        assert resolve_image_info(_jpeg(800, 0), "jpeg") == ("jpeg", None)

    def test_jpg_alias_is_accepted(self) -> None:
        assert resolve_image_info(_jpeg(400, 300), "jpg") == ("jpeg", (400, 300))


class TestPng:
    def test_png_dimensions(self) -> None:
        assert resolve_image_info(_png(1024, 768), "png") == ("png", (1024, 768))

    def test_missing_ihdr_returns_no_dimensions(self) -> None:
        bad = b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IDAT" + b"\x00" * 13
        assert resolve_image_info(bad, "png") == ("png", None)

    def test_truncated_png_returns_no_dimensions(self) -> None:
        assert resolve_image_info(_png(1024, 768)[:20], "png") == ("png", None)

    def test_non_png_bytes_return_no_dimensions(self) -> None:
        assert resolve_image_info(b"definitely not png", "png") == ("png", None)

    def test_zero_dimension_returns_no_dimensions(self) -> None:
        assert resolve_image_info(_png(0, 512), "png") == ("png", None)


class TestGif:
    def test_gif89a_dimensions(self) -> None:
        assert resolve_image_info(_gif(160, 120), "gif") == ("gif", (160, 120))

    def test_gif87a_dimensions(self) -> None:
        assert resolve_image_info(_gif(80, 60, version=b"87a"), "gif") == (
            "gif",
            (80, 60),
        )

    def test_truncated_gif_returns_no_dimensions(self) -> None:
        assert resolve_image_info(_gif(160, 120)[:9], "gif") == ("gif", None)

    def test_non_gif_bytes_return_no_dimensions(self) -> None:
        assert resolve_image_info(b"nope", "gif") == ("gif", None)

    def test_zero_dimension_returns_no_dimensions(self) -> None:
        assert resolve_image_info(_gif(0, 60), "gif") == ("gif", None)


class TestFormatSniffFallback:
    """The declared format disagrees with the payload's magic bytes.

    The sniff wins, so the returned format — and therefore the label and
    size built from it — describes the bytes actually served, never the
    mislabeled header.
    """

    def test_png_payload_declared_as_jpeg(self) -> None:
        assert resolve_image_info(_png(640, 480), "jpeg") == ("png", (640, 480))

    def test_jpeg_payload_declared_as_png(self) -> None:
        assert resolve_image_info(_jpeg(800, 600), "png") == ("jpeg", (800, 600))

    def test_gif_payload_declared_as_jpeg(self) -> None:
        assert resolve_image_info(_gif(320, 240), "jpeg") == ("gif", (320, 240))

    def test_unknown_declared_format_still_sniffs(self) -> None:
        assert resolve_image_info(_png(512, 512), "webp") == ("png", (512, 512))

    def test_unknown_format_and_unknown_magic_keeps_declaration(self) -> None:
        assert resolve_image_info(b"\x00\x01\x02\x03", "webp") == ("webp", None)
        assert resolve_image_info(b"\x00\x01\x02\x03", "") == ("", None)


class TestRandomPayloadsNeverRaise:
    """ "Parsers run on untrusted network bytes: never raise.

    The seeded loops feed each magic prefix a fresh batch of pseudo-random
    bytes. They guard the bounds handling — a regression that let a corrupt
    segment length index past the end of the buffer would surface here —
    while the zero-dimension tests above pin the other half of the contract:
    when dimensions are reported, both are positive.
    """

    def test_jpeg_prefix_random_bytes(self) -> None:
        for seed in range(32):
            data = b"\xff\xd8" + random.Random(seed).randbytes(1024)
            fmt, dims = resolve_image_info(data, "jpeg")
            assert fmt == "jpeg"
            assert dims is None or (dims[0] > 0 and dims[1] > 0), f"seed {seed}"

    def test_png_prefix_random_bytes(self) -> None:
        for seed in range(32):
            data = b"\x89PNG\r\n\x1a\n" + random.Random(seed).randbytes(512)
            fmt, dims = resolve_image_info(data, "png")
            assert fmt == "png"
            assert dims is None or (dims[0] > 0 and dims[1] > 0), f"seed {seed}"

    def test_gif_prefix_random_bytes(self) -> None:
        for seed in range(32):
            data = b"GIF89a" + random.Random(seed).randbytes(256)
            fmt, dims = resolve_image_info(data, "gif")
            assert fmt == "gif"
            assert dims is None or (dims[0] > 0 and dims[1] > 0), f"seed {seed}"
