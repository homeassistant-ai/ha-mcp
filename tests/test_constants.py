"""
Test constants shared across test modules.

This module centralizes test configuration values to ensure consistency
across all test environments.
"""

# Long-lived access token for test Home Assistant instance
# This token is embedded in tests/initial_test_state/.storage/auth
# Expires: 2035 (10+ years from token creation)
TEST_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiIxOTE5ZTZlMTVkYjI0Mzk2YTQ4YjFiZTI1MDM1YmU2YSIsImlhdCI6MTc1NzI4OTc5NiwiZXhwIjoyMDcyNjQ5Nzk2fQ.Yp9SSAjm2gvl9Xcu96FFxS8SapHxWAVzaI0E3cD9xac"

# LLAT for a seeded non-admin user (``system-users`` group). Used to
# verify the admin gate on ``ha_mcp_tools.get_caller_token`` rejects
# non-admin callers end-to-end. The user, refresh_token, and jwt_key
# entries that back this JWT live alongside the admin entries in
# ``tests/initial_test_state/.storage/auth``.
NON_ADMIN_TEST_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiIzNzkyOTc5YzdiYTdlNjAwMDU2NjJlMjJjNzgzOGNmOCIsImlhdCI6MTc1NzI4OTc5NiwiZXhwIjoyMDcyNjQ5Nzk2fQ.LGH1CMs-3ML7YkY14FctNiHSenDGLjjW8wAB_MlZ7VU"

# Home Assistant Docker image for E2E/performance/UAT tests.
# Keep in sync with .github/workflows/e2e-tests.yml and pr.yml.
# renovate: datasource=docker depName=ghcr.io/home-assistant/home-assistant
HA_TEST_IMAGE = "ghcr.io/home-assistant/home-assistant:2026.5.4"

# Test user credentials (for UI access)
TEST_USER = "mcp"
TEST_PASSWORD = "mcp"
