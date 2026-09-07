# Test-only mock screenshot engine for the HAOS e2e bake. Mirrors balloob's
# Puppet HTTP contract (GET /<path>?viewport=WxH&format=png -> image/png)
# WITHOUT Chromium: it returns a synthetic, viewport-sized PNG and gates
# rendering on validating the configured access_token against Home Assistant
# (a bad token fails, exactly like the real engine reaching only the login
# screen). python:3.13-slim is the same fast, reliable base the dev +
# webhook-proxy add-ons build from in this bake. No apt, no npm, no Chromium.
# Digest-pinned (tracks the coordinated Python base every managed Dockerfile
# shares -- see the "python" packageRule in renovate.json) so the
# in-Supervisor build is reproducible and Renovate-bumpable. The
# webhook-proxy add-ons are excluded from that automation and update only
# through their own dev-first promotion workflow, so their pin can lag this
# one. An unpinned tag here would reintroduce the floating-base bake failure
# this change exists to eliminate.
FROM python:3.13-slim@sha256:9d2e5553305c7c7b0097999bb17187c69b921ccd6bc9d40e4bb5ebe652c00285
COPY server.py /server.py
CMD ["python", "/server.py"]
