# Test-only mock screenshot engine for the HAOS e2e bake. Mirrors balloob's
# Puppet HTTP contract (GET /<path>?viewport=WxH&format=png -> image/png)
# WITHOUT Chromium: it returns a synthetic, viewport-sized PNG and gates
# rendering on validating the configured access_token against Home Assistant
# (a bad token fails, exactly like the real engine reaching only the login
# screen). python:3.13-slim is the same fast, reliable base the dev +
# webhook-proxy add-ons build from in this bake. No apt, no npm, no Chromium.
# Digest-pinned (matches the webhook-proxy addon) so the in-Supervisor build is
# reproducible and Renovate-bumpable — an unpinned tag would reintroduce the
# floating-base bake failure this change exists to eliminate.
FROM python:3.13-slim@sha256:3de9a8d7aedbb7984dc18f2dff178a7850f16c1ae7c34ba9d7ecc23d0755e35f
COPY server.py /server.py
CMD ["python", "/server.py"]
