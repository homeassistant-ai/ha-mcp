#!/usr/bin/env bash
set -euo pipefail

# Run E2E Tests with Container2WASM
# This script converts Home Assistant to WASM and runs e2e tests against it
# Requires: Docker, wasmtime

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Configuration
C2W_VERSION="${C2W_VERSION:-v0.8.3}"
HA_IMAGE="${HA_IMAGE:-ghcr.io/home-assistant/home-assistant:stable}"
WASM_OUTPUT="${WASM_OUTPUT:-/tmp/homeassistant.wasm}"
HA_CONFIG_DIR="${HA_CONFIG_DIR:-/tmp/ha-wasm-config}"
HA_PORT="${HA_PORT:-8123}"

echo "=================================================="
echo "🧪 Home Assistant MCP E2E Tests with WASM"
echo "=================================================="
echo ""

# Check prerequisites
check_prerequisites() {
    echo "🔍 Checking prerequisites..."

    if ! command -v docker &> /dev/null; then
        echo "❌ Docker is not installed. Please install Docker first."
        exit 1
    fi

    if ! command -v c2w &> /dev/null; then
        echo "📦 container2wasm (c2w) not found. Installing..."
        install_c2w
    else
        echo "✅ container2wasm: $(c2w --version)"
    fi

    if ! command -v wasmtime &> /dev/null; then
        echo "📦 wasmtime not found. Installing..."
        install_wasmtime
    else
        echo "✅ wasmtime: $(wasmtime --version)"
    fi

    echo ""
}

# Install container2wasm
install_c2w() {
    local temp_dir=$(mktemp -d)
    cd "$temp_dir"

    echo "Downloading container2wasm ${C2W_VERSION}..."
    curl -L "https://github.com/container2wasm/container2wasm/releases/download/${C2W_VERSION}/container2wasm-${C2W_VERSION}-linux-amd64.tar.gz" -o c2w.tar.gz

    tar -xzf c2w.tar.gz
    sudo mv c2w /usr/local/bin/
    sudo mv c2w-net /usr/local/bin/

    cd -
    rm -rf "$temp_dir"

    echo "✅ container2wasm installed: $(c2w --version)"
}

# Install wasmtime
install_wasmtime() {
    curl https://wasmtime.dev/install.sh -sSf | bash
    export PATH="$HOME/.wasmtime/bin:$PATH"
    echo "✅ wasmtime installed: $(wasmtime --version)"
}

# Pull Home Assistant image
pull_ha_image() {
    echo "🏠 Pulling Home Assistant image: ${HA_IMAGE}..."
    docker pull "${HA_IMAGE}"
    echo "✅ Image pulled"
    echo ""
}

# Convert to WASM
convert_to_wasm() {
    echo "🔄 Converting Home Assistant to WASM..."
    echo "Image: ${HA_IMAGE}"
    echo "Output: ${WASM_OUTPUT}"
    echo "⚠️  This may take 5-15 minutes depending on your system..."
    echo ""

    if [ -f "${WASM_OUTPUT}" ]; then
        echo "ℹ️  WASM file already exists at ${WASM_OUTPUT}"
        read -p "Do you want to re-convert? (y/N): " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            echo "✅ Using existing WASM file"
            return
        fi
        rm "${WASM_OUTPUT}"
    fi

    c2w "${HA_IMAGE}" "${WASM_OUTPUT}"

    echo ""
    echo "✅ Conversion complete!"
    echo "WASM file size: $(ls -lh ${WASM_OUTPUT} | awk '{print $5}')"
    echo ""
}

# Prepare Home Assistant configuration
prepare_config() {
    echo "📁 Preparing Home Assistant configuration..."

    rm -rf "${HA_CONFIG_DIR}"
    mkdir -p "${HA_CONFIG_DIR}"

    if [ -d "${PROJECT_ROOT}/tests/initial_test_state" ]; then
        cp -r "${PROJECT_ROOT}/tests/initial_test_state"/* "${HA_CONFIG_DIR}/"
        echo "✅ Configuration copied from tests/initial_test_state"
    else
        echo "⚠️  Warning: tests/initial_test_state not found"
        echo "Creating minimal configuration..."
        cat > "${HA_CONFIG_DIR}/configuration.yaml" <<EOF
# Minimal Home Assistant configuration
default_config:

http:
  server_port: ${HA_PORT}

logger:
  default: info
EOF
    fi

    chmod -R 755 "${HA_CONFIG_DIR}"
    echo ""
}

# Start WASM runtime
start_wasm_runtime() {
    echo "🚀 Starting Home Assistant in WASM runtime..."
    echo "Port: ${HA_PORT}"
    echo "Config: ${HA_CONFIG_DIR}"
    echo ""

    # Note: wasmtime networking and directory mapping may need adjustments
    # based on container2wasm's actual WASM output capabilities

    export PATH="$HOME/.wasmtime/bin:$PATH"

    echo "⚠️  Note: WASM networking support is experimental"
    echo "    This may require additional configuration"
    echo ""

    # Start wasmtime in background
    # Adjust these flags based on actual WASM requirements
    wasmtime run \
        --tcplisten "localhost:${HA_PORT}" \
        --mapdir "/config::${HA_CONFIG_DIR}" \
        --env "TZ=UTC" \
        "${WASM_OUTPUT}" &

    WASM_PID=$!
    echo "WASM_PID=${WASM_PID}" > /tmp/ha-wasm.pid

    echo "⏳ Waiting for Home Assistant to initialize..."
    sleep 30

    # Wait for API
    wait_for_api
}

# Wait for Home Assistant API
wait_for_api() {
    echo "🔍 Checking Home Assistant API readiness..."
    local max_attempts=60
    local attempt=1

    while [ $attempt -le $max_attempts ]; do
        if curl -sf "http://localhost:${HA_PORT}/api/" > /dev/null 2>&1; then
            echo "✅ Home Assistant API is ready!"
            echo ""
            return 0
        fi

        if [ $((attempt % 10)) -eq 0 ]; then
            echo "Attempt ${attempt}/${max_attempts}: Still waiting..."
        fi

        sleep 2
        attempt=$((attempt + 1))
    done

    echo "❌ Home Assistant API failed to start within timeout"
    stop_wasm_runtime
    exit 1
}

# Run E2E tests
run_tests() {
    echo "🧪 Running E2E tests..."
    echo ""

    cd "${PROJECT_ROOT}"

    export HOMEASSISTANT_URL="http://localhost:${HA_PORT}"
    export HOMEASSISTANT_TOKEN="test-token"

    # Run fast tests only (skip slow ones)
    uv run pytest tests/src/e2e/ \
        -v \
        --tb=short \
        -m "not slow" \
        "$@"

    local exit_code=$?
    echo ""

    if [ $exit_code -eq 0 ]; then
        echo "✅ All tests passed!"
    else
        echo "❌ Some tests failed"
    fi

    return $exit_code
}

# Stop WASM runtime
stop_wasm_runtime() {
    if [ -f /tmp/ha-wasm.pid ]; then
        local pid=$(cat /tmp/ha-wasm.pid | grep WASM_PID | cut -d= -f2)
        if [ -n "$pid" ]; then
            echo "🛑 Stopping WASM runtime (PID: $pid)..."
            kill "$pid" 2>/dev/null || true
            wait "$pid" 2>/dev/null || true
        fi
        rm /tmp/ha-wasm.pid
    fi
}

# Cleanup on exit
cleanup() {
    echo ""
    echo "🧹 Cleaning up..."
    stop_wasm_runtime
}

trap cleanup EXIT

# Main execution
main() {
    check_prerequisites
    pull_ha_image
    convert_to_wasm
    prepare_config
    start_wasm_runtime
    run_tests "$@"
}

# Run main function with all arguments
main "$@"
