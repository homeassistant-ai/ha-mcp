# Runtime hook to ensure codecs are registered before any imports
# This runs before the main application starts

# Register the idna codec (required for httpx URL parsing).
# Imported purely for its registration side effect, not for any bound name.
try:
    import idna.codec  # noqa: F401 - This registers the 'idna' codec
except ImportError:
    # idna may not be bundled in every build; httpx falls back to its own
    # IDNA handling, so a missing codec here is non-fatal.
    pass

# Ensure encodings are available (imported for its registration side effect).
try:
    import encodings.idna  # noqa: F401
except ImportError:
    # encodings.idna is part of the stdlib but may be excluded by a trimmed
    # PyInstaller build; absence is non-fatal for normal ASCII hostnames.
    pass
