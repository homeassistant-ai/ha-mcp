# Runtime hook to ensure codecs are registered before any imports
# This runs before the main application starts

# Register the idna codec (required for httpx URL parsing)
try:
    import idna.codec  # noqa: F401 - This registers the 'idna' codec
except ImportError:
    pass

# Ensure encodings are available
try:
    import encodings.idna  # noqa: F401
except ImportError:
    pass
