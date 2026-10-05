"""Resolve basic facts (format, dimensions) from raw image bytes.

Pure-stdlib header parsing — no image library required. Camera snapshots
are the target: JPEG (the camera proxy default), PNG, and GIF. The
resolver always returns a format together with the dimensions, so the
reported label can never describe different bytes than the size. Every
parse path is best-effort: undecodable payloads yield no dimensions,
and callers must degrade gracefully rather than fail the request.
"""

# Every T.81 start-of-frame (SOF) marker — 0xC0 through 0xCF across the
# sequential, progressive, lossless and differential coding modes — minus
# the three non-SOF markers that share the range: DHT (0xC4), JPG (0xC8)
# and DAC (0xCC). The walker must skip those, not stop at them: a DHT
# commonly precedes the frame, and misparsing its short header as a frame
# definition would report garbage dimensions.
_JPEG_SOF_MARKERS = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_GIF_SIGNATURES = (b"GIF87a", b"GIF89a")


def resolve_image_info(
    data: bytes, declared_format: str
) -> tuple[str, tuple[int, int] | None]:
    """Resolve the payload's actual format and its dimensions.

    The magic bytes decide the format: a recognized container (JPEG, PNG
    or GIF) wins over *declared_format*, so a mislabeled Content-Type can
    never make the reported label disagree with the served bytes. When no
    magic matches, *declared_format* is reported as-is with no
    dimensions, so the text block can still name the format the header
    claimed.
    """
    if data[:2] == b"\xff\xd8":
        return "jpeg", _jpeg_dimensions(data)
    if data[:8] == _PNG_SIGNATURE:
        return "png", _png_dimensions(data)
    if data[:6] in _GIF_SIGNATURES:
        return "gif", _gif_dimensions(data)
    # No recognized container: keep the declared name, report no size.
    return declared_format, None


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    """Walk JPEG segments to the first SOF marker, which carries the size.

    The dispatcher has already verified the two-byte SOI; every further
    read is bounds-checked by the walk, so a truncated or corrupt header
    yields ``None`` rather than an exception. Only the header is parsed;
    the compressed scan data is never decoded. The declared segment
    length is validated before the dimension fields are read, so a
    corrupt length can neither reach past the end of the buffer nor
    report bytes outside the segment as the image size.
    """
    sof_pos = _find_sof_position(data)
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


def _skip_fill_bytes(data: bytes, pos: int) -> int:
    """Advance *pos* past any 0xFF fill bytes after it (T.81 B.1.1.2).

    Encoders may emit any number of 0xFF bytes between markers to align
    the stream; each is consumed so the next marker read is a real one.
    Stops at the first non-0xFF byte, or at the buffer end.
    """
    size = len(data)
    while data[pos + 1] == 0xFF and pos + 3 <= size:
        pos += 1
    return pos


def _find_sof_position(data: bytes) -> int | None:
    """Advance through the JPEG segments after the SOI to the first SOF marker.

    Returns the offset of the SOF marker, or ``None`` when the header is
    malformed, truncated, or ends (EOI) without a frame definition.
    """
    size = len(data)
    pos = 2  # first segment after the two-byte SOI
    while pos + 2 <= size:
        if data[pos] != 0xFF:
            return None
        pos = _skip_fill_bytes(data, pos)
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
