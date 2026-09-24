"""Unit tests for ha_mcp.image_info (header-only image dimension resolution).

Fixtures are hand-built byte literals — the parser only reads headers, so
no image encoder is needed and the tests stay dependency-free. The JPEG
fixtures include an APP1 "Exif" segment before the SOF to exercise the
segment walker against a realistic camera payload.
"""

from ha_mcp.image_info import read_image_dimensions


def _jpeg(height: int, width: int, extra_segments: bytes = b"") -> bytes:
    """Build a minimal structurally-valid JPEG with the given dimensions.

    Segment length fields include their own two bytes (the JPEG spec's
    ``Lf``), matching what real camera firmware emits.
    """
    app1_payload = b"Exif\x00\x00" + b"\x00" * 16
    app1 = b"\xff\xe1" + (len(app1_payload) + 2).to_bytes(2, "big") + app1_payload
    sof0 = (
        b"\xff\xc0"
        + (11).to_bytes(2, "big")
        + b"\x08"  # bit depth
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + b"\x01"  # one component
        + b"\x01"  # component: id 1, 1x1 sampling, table 0
    )
    return b"\xff\xd8" + extra_segments + app1 + sof0 + b"\xff\xd9"


def _png(width: int, height: int) -> bytes:
    """Build a minimal structurally-valid PNG with the given dimensions."""
    ihdr_payload = (
        width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x06\x00\x00\x00"  # 8-bit RGB, no interlace
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
        assert read_image_dimensions(_jpeg(600, 800), "jpeg") == (800, 600)

    def test_walks_past_exif_app1_segment(self) -> None:
        # A realistic camera JPEG: APP1 "Exif" then SOF0.
        jpeg = _jpeg(720, 1440)
        assert read_image_dimensions(jpeg, "jpeg") == (1440, 720)

    def test_walks_past_restart_markers(self) -> None:
        jpeg = _jpeg(100, 200, extra_segments=b"\xff\xd0" + b"\xff\xd1")
        assert read_image_dimensions(jpeg, "jpeg") == (200, 100)

    def test_no_sof_marker_returns_none(self) -> None:
        jpeg = b"\xff\xd8" + b"\xff\xe1" + (6).to_bytes(2, "big") + b"Exif"
        assert read_image_dimensions(jpeg, "jpeg") is None

    def test_truncated_sof_returns_none(self) -> None:
        # SOF marker + length present, payload cut off.
        jpeg = b"\xff\xd8" + b"\xff\xc0" + (9).to_bytes(2, "big") + b"\x08\x02"
        assert read_image_dimensions(jpeg, "jpeg") is None

    def test_non_jpeg_bytes_return_none(self) -> None:
        assert read_image_dimensions(b"not an image at all", "jpeg") is None

    def test_zero_dimension_returns_none(self) -> None:
        assert read_image_dimensions(_jpeg(0, 800), "jpeg") is None

    def test_jpg_alias_is_accepted(self) -> None:
        assert read_image_dimensions(_jpeg(300, 400), "jpg") == (400, 300)


class TestPng:
    def test_png_dimensions(self) -> None:
        assert read_image_dimensions(_png(1024, 768), "png") == (1024, 768)

    def test_missing_ihdr_returns_none(self) -> None:
        bad = b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IDAT" + b"\x00" * 13
        assert read_image_dimensions(bad, "png") is None

    def test_truncated_png_returns_none(self) -> None:
        assert read_image_dimensions(_png(1024, 768)[:20], "png") is None

    def test_non_png_bytes_return_none(self) -> None:
        assert read_image_dimensions(b"definitely not png", "png") is None

    def test_zero_dimension_returns_none(self) -> None:
        assert read_image_dimensions(_png(0, 512), "png") is None


class TestGif:
    def test_gif89a_dimensions(self) -> None:
        assert read_image_dimensions(_gif(160, 120), "gif") == (160, 120)

    def test_gif87a_dimensions(self) -> None:
        assert read_image_dimensions(_gif(80, 60, version=b"87a"), "gif") == (80, 60)

    def test_truncated_gif_returns_none(self) -> None:
        assert read_image_dimensions(_gif(160, 120)[:9], "gif") is None

    def test_non_gif_bytes_return_none(self) -> None:
        assert read_image_dimensions(b"nope", "gif") is None

    def test_zero_dimension_returns_none(self) -> None:
        assert read_image_dimensions(_gif(0, 60), "gif") is None


class TestFormatSniffFallback:
    """Declared format disagrees with the payload — trust the magic bytes win."""

    def test_png_payload_declared_as_jpeg(self) -> None:
        assert read_image_dimensions(_png(640, 480), "jpeg") == (640, 480)

    def test_jpeg_payload_declared_as_png(self) -> None:
        assert read_image_dimensions(_jpeg(600, 800), "png") == (800, 600)

    def test_gif_payload_declared_as_jpeg(self) -> None:
        assert read_image_dimensions(_gif(320, 240), "jpeg") == (320, 240)

    def test_unknown_declared_format_still_sniffs(self) -> None:
        assert read_image_dimensions(_png(512, 512), "webp") == (512, 512)

    def test_unknown_format_and_unknown_magic_returns_none(self) -> None:
        assert read_image_dimensions(b"\x00\x01\x02\x03", "webp") is None
        assert read_image_dimensions(b"\x00\x01\x02\x03", "") is None
