"""
Test constants shared across test modules.

This module centralizes test configuration values to ensure consistency
across all test environments.
"""

# Long-lived access token for test Home Assistant instance
# This token is embedded in tests/initial_test_state/.storage/auth
# Expires: 2035 (10+ years from token creation)
TEST_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiIxOTE5ZTZlMTVkYjI0Mzk2YTQ4YjFiZTI1MDM1YmU2YSIsImlhdCI6MTc1NzI4OTc5NiwiZXhwIjoyMDcyNjQ5Nzk2fQ.Yp9SSAjm2gvl9Xcu96FFxS8SapHxWAVzaI0E3cD9xac"

# Test user credentials (for UI access)
TEST_USER = "mcp"
TEST_PASSWORD = "mcp"
