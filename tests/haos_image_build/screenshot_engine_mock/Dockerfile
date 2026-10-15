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
# PYTHON_BASE lets CI point the runtime base at another registry holding the
# same digest when ECR Public rate-limits the pull; the default is the pin.
ARG PYTHON_BASE=public.ecr.aws/docker/library/python:3.13-slim@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f
FROM ${PYTHON_BASE}
COPY server.py /server.py
CMD ["python", "/server.py"]
