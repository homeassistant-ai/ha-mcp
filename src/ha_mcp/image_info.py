"""Resolve basic facts (dimensions) from raw image bytes.

Pure-stdlib header parsing — no image library required. Camera snapshots
are the target: JPEG (the camera proxy default), PNG, and GIF. Every parse
path is best-effort: callers treat ``None`` as "dimensions could not be
determined" and must degrade gracefully rather than fail the request.
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


def read_image_dimensions(data: bytes, image_format: str) -> tuple[int, int] | None:
    """Return ``(width, height)`` for *data*, or ``None`` when undetermined.

    Dispatches on the declared *image_format* (from the Content-Type
    header) but verifies the payload's magic bytes: when a camera
    mislabels its format, the sniff fallback parses whatever the bytes
    actually are.
    """
    fmt = (image_format or "").lower()
    if fmt in ("jpeg", "jpg"):
        dims = _jpeg_dimensions(data)
    elif fmt == "png":
        dims = _png_dimensions(data)
    elif fmt == "gif":
        dims = _gif_dimensions(data)
    else:
        dims = None
    if dims is not None:
        return dims
    # Declared format and payload disagree — trust the magic bytes.
    if data[:2] == b"\xff\xd8":
        return _jpeg_dimensions(data)
    if data[:8] == _PNG_SIGNATURE:
        return _png_dimensions(data)
    if data[:6] in _GIF_SIGNATURES:
        return _gif_dimensions(data)
    return None


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    """Walk JPEG segments to the first SOF marker, which carries the size.

    Only the header is parsed; the compressed scan data is never decoded.
Check the SOF segment length before reading dimensions    The declared segment length is validated before the dimension fields
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
