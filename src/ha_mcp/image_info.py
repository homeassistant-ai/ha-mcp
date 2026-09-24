"""Resolve basic facts (format, dimensions) from raw image bytes.

Pure-stdlib header parsing — no image library required. Camera snapshots
are the target: JPEG (the camera proxy default), PNG, and GIF. The
resolver always returns a format together with the dimensions, so the
reported label can never describe different bytes than the size. Every
parse path is best-effort: undecodable payloads yield no dimensions,
and callers must degrade gracefully rather than fail the request.
"""

_JPEG_SOF_MARKERS = frozenset(
    (
        0xC0,
        0xC1,
        0xC2,
        0xC3,  # baseline / extended sequential DCT
        0xC5,
        0xC6,
        0xC7,  # progressive DCT
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,  # operation / expert modes
    )
)

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_GIF_SIGNATURES = (b"GIF87a", b"GIF89a")


def resolve_image_info(
    data: bytes, declared_format: str
) -> tuple[str, tuple[int, int] | None]:
    """Resolve the payload's actual format and its dimensions.

    Dispatches on the declared *declared_format* (from the Content-Type
    header) but verifies the payload's magic bytes: when a camera
    mislabels its format, the sniff result wins, so the returned format
    and dimensions always describe the same bytes. When no sniff
    matches, the declared format is kept and no dimensions are
    reported.
    """
    fmt = (declared_format or "").lower()
    if fmt in ("jpeg", "jpg"):
        if data[:2] == b"\xff\xd8":
            return "jpeg", _jpeg_dimensions(data)
    elif fmt == "png":
        if data[:8] == _PNG_SIGNATURE:
            return fmt, _png_dimensions(data)
    elif fmt == "gif":
        if data[:6] in _GIF_SIGNATURES:
            return fmt, _gif_dimensions(data)
    # Declared format and payload disagree — trust the magic bytes.
    if data[:2] == b"\xff\xd8":
        return "jpeg", _jpeg_dimensions(data)
    if data[:8] == _PNG_SIGNATURE:
        return "png", _png_dimensions(data)
    if data[:6] in _GIF_SIGNATURES:
        return "gif", _gif_dimensions(data)
    return fmt, None


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    """Walk JPEG segments to the first SOF marker, which carries the size.

    Only the header is parsed; the compressed scan data is never decoded.
    The declared segment length is validated before the dimension fields
    are read, so a corrupt length can neither reach past the end of the
    buffer nor report bytes outside the segment as the image size.
    """
    if len(data) < 4 or data[0] != 0xFF or data[1] != 0xD8:
        return None
    sof_pos = _find_sof_position(data, 2)
    if sof_pos is None:
        return None
    # The SOF length field counts itself, so the segment spans
    # ``2 + length`` bytes from the marker, and the dimension fields sit
    # at offsets 5..8 — the segment must therefore declare at least 7
    # bytes.
    length = (data[sof_pos + 2] << 8) | data[sof_pos + 3]
    if length < 7:
        return None
    if sof_pos + 2 + length > len(data):
        return None
    height = (data[sof_pos + 5] << 8) | data[sof_pos + 6]
    width = (data[sof_pos + 7] << 8) | data[sof_pos + 8]
    if width == 0 or height == 0:
        return None
    return width, height


def _find_sof_position(data: bytes, pos: int) -> int | None:
    """Advance through JPEG segments from *pos* to the first SOF marker.

    Returns the offset of the SOF marker, or ``None`` when the header is
    malformed, truncated, or ends (EOI) without a frame definition.
    """
    size = len(data)
    while pos + 2 <= size:
        if data[pos] != 0xFF:
            return None
        marker = data[pos + 1]
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:  # no payload
            pos += 2
            continue
        if marker == 0xD9 or pos + 4 > size:
            return None
        length = (data[pos + 2] << 8) | data[pos + 3]
        if length < 2:
            return None
        if marker in _JPEG_SOF_MARKERS:
            return pos
        pos += 2 + length
    return None


def _png_dimensions(data: bytes) -> tuple[int, int] | None:
    """Read the IHDR chunk, which is always first and carries the size."""
    if len(data) < 24 or data[:8] != _PNG_SIGNATURE:
        return None
    if data[12:16] != b"IHDR":
        return None
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    if width == 0 or height == 0:
        return None
    return width, height


def _gif_dimensions(data: bytes) -> tuple[int, int] | None:
    """Read the logical screen width/height from the GIF header."""
    if len(data) < 10 or data[:6] not in _GIF_SIGNATURES:
        return None
    width = int.from_bytes(data[6:8], "little")
    height = int.from_bytes(data[8:10], "little")
    if width == 0 or height == 0:
        return None
    return width, height
