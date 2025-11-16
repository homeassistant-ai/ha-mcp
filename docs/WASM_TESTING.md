# Container2WASM Integration for E2E Testing

This document describes how to run Home Assistant MCP Server e2e tests using container2wasm, which converts Docker containers to WebAssembly modules.

## 🎯 Overview

The project now supports running e2e tests against a WASM-based Home Assistant instance using [container2wasm](https://github.com/container2wasm/container2wasm). This enables:

- **Browser-based testing**: Run Home Assistant in web browsers
- **Lightweight deployment**: WASM binaries are more portable than containers
- **Edge computing**: Deploy Home Assistant to edge devices with WASM runtimes
- **Testing flexibility**: Run tests without Docker daemon requirements (once converted)

## 🔧 Prerequisites

### Required Tools

1. **Docker** (for container conversion)
   ```bash
   # Verify Docker is installed
   docker --version
   ```

2. **container2wasm (c2w)** - Installs automatically via script
   ```bash
   # Or install manually
   curl -L https://github.com/container2wasm/container2wasm/releases/download/v0.8.3/container2wasm-v0.8.3-linux-amd64.tar.gz -o c2w.tar.gz
   tar -xzf c2w.tar.gz
   sudo mv c2w c2w-net /usr/local/bin/
   ```

3. **wasmtime** - Installs automatically via script
   ```bash
   # Or install manually
   curl https://wasmtime.dev/install.sh -sSf | bash
   ```

### System Requirements

- Linux or macOS (Windows via WSL2)
- 8GB+ RAM (conversion is memory-intensive)
- 10GB+ free disk space
- Docker daemon running

## 🚀 Quick Start

### Option 1: Automated Script (Recommended)

```bash
# Run the automated WASM test script
./scripts/run-e2e-with-wasm.sh

# Run specific tests
./scripts/run-e2e-with-wasm.sh tests/src/e2e/basic/

# Run with custom configuration
HA_IMAGE="ghcr.io/home-assistant/home-assistant:2024.1" \
  HA_PORT=9123 \
  ./scripts/run-e2e-with-wasm.sh
```

The script will:
1. ✅ Check prerequisites (install c2w and wasmtime if needed)
2. 🏠 Pull Home Assistant Docker image
3. 🔄 Convert to WASM (may take 5-15 minutes)
4. 📁 Prepare test configuration
5. 🚀 Start WASM runtime
6. 🧪 Run e2e tests
7. 🧹 Cleanup on exit

### Option 2: Manual Steps

```bash
# 1. Pull Home Assistant image
docker pull ghcr.io/home-assistant/home-assistant:stable

# 2. Convert to WASM (this takes time!)
c2w ghcr.io/home-assistant/home-assistant:stable /tmp/homeassistant.wasm

# 3. Prepare configuration
mkdir -p /tmp/ha-config
cp -r tests/initial_test_state/* /tmp/ha-config/

# 4. Start WASM runtime
wasmtime run \
  --tcplisten localhost:8123 \
  --mapdir /config::/tmp/ha-config \
  --env TZ=UTC \
  /tmp/homeassistant.wasm &

# 5. Wait for startup
sleep 30

# 6. Run tests
HOMEASSISTANT_URL=http://localhost:8123 \
  uv run pytest tests/src/e2e/ -v --tb=short -m "not slow"

# 7. Cleanup
kill %1
```

## 🔄 GitHub Actions Workflow

The project includes a GitHub Actions workflow that automatically runs e2e tests with WASM:

**.github/workflows/e2e-tests-wasm.yml**

Triggered on:
- Pushes to `main`/`master` branches
- Pull requests affecting test code
- Manual workflow dispatch

The workflow:
1. Sets up the environment (Docker, Python, uv)
2. Installs container2wasm and wasmtime
3. Converts Home Assistant to WASM
4. Runs e2e tests against the WASM runtime
5. Uploads WASM artifact on failure for debugging

### Running Workflow Manually

```bash
# Via GitHub CLI
gh workflow run e2e-tests-wasm.yml

# Via GitHub UI
# Navigate to Actions → E2E Tests with Container2WASM → Run workflow
```

## ⚙️ Configuration Options

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `C2W_VERSION` | `v0.8.3` | container2wasm version to use |
| `HA_IMAGE` | `ghcr.io/home-assistant/home-assistant:stable` | Docker image to convert |
| `WASM_OUTPUT` | `/tmp/homeassistant.wasm` | Output path for WASM file |
| `HA_CONFIG_DIR` | `/tmp/ha-wasm-config` | Home Assistant config directory |
| `HA_PORT` | `8123` | Port for Home Assistant API |
| `HOMEASSISTANT_URL` | `http://localhost:8123` | URL for tests to connect to |
| `HOMEASSISTANT_TOKEN` | `test-token` | Authentication token |

### Custom Home Assistant Version

```bash
# Test against specific version
HA_IMAGE="ghcr.io/home-assistant/home-assistant:2024.1.0" \
  ./scripts/run-e2e-with-wasm.sh

# Test against dev version
HA_IMAGE="ghcr.io/home-assistant/home-assistant:dev" \
  ./scripts/run-e2e-with-wasm.sh
```

## 📊 Performance Considerations

### Conversion Time
- **First conversion**: 5-15 minutes (depends on system)
- **Cached images**: 2-5 minutes
- **WASM file size**: ~500MB-1GB (varies by HA version)

### Runtime Performance
- **Startup time**: 30-60 seconds (vs 15-30s for Docker)
- **API response**: Similar to Docker (CPU emulation overhead)
- **Memory usage**: Higher due to emulation layer

### Optimization Tips

1. **Reuse WASM files**: Once converted, reuse the WASM file for multiple test runs
2. **Cache artifacts**: Store WASM files in GitHub Actions cache
3. **Parallel testing**: Run tests in parallel with pytest-xdist
4. **Fast tests only**: Use `-m "not slow"` to skip long-running tests

## 🐛 Troubleshooting

### Conversion Fails

```bash
# Check Docker is running
docker ps

# Check disk space
df -h /tmp

# Verify image exists
docker pull ghcr.io/home-assistant/home-assistant:stable
```

### WASM Runtime Fails to Start

```bash
# Check wasmtime installation
wasmtime --version

# Verify WASM file
ls -lh /tmp/homeassistant.wasm

# Check logs
wasmtime run --log-level=debug /tmp/homeassistant.wasm
```

### API Not Responding

```bash
# Check if process is running
ps aux | grep wasmtime

# Test API manually
curl http://localhost:8123/api/

# Check port availability
lsof -i :8123
```

### Tests Fail

```bash
# Run single test for debugging
HOMEASSISTANT_URL=http://localhost:8123 \
  uv run pytest tests/src/e2e/basic/test_connection.py -v

# Check Home Assistant logs (if accessible)
# Access web UI at http://localhost:8123
```

## 🔬 Technical Details

### How Container2WASM Works

1. **CPU Emulation**: Uses Bochs (x86_64) or TinyEMU (RISC-V) compiled to WASM
2. **Guest OS**: Linux kernel runs on emulated CPU with runc for containers
3. **Filesystem**: WASI APIs expose host directories via virtio-9p
4. **Networking**: Experimental support via wasmtime's `--tcplisten`

### Architecture

```
┌─────────────────────────────────────────┐
│  E2E Tests (Python/pytest)              │
│  ↓ HTTP/WebSocket                       │
├─────────────────────────────────────────┤
│  Home Assistant API                     │
│  (running in WASM)                      │
│  ↓                                      │
├─────────────────────────────────────────┤
│  Wasmtime Runtime                       │
│  - WASM execution                       │
│  - Network mapping (--tcplisten)        │
│  - Directory mapping (--mapdir)         │
│  ↓                                      │
├─────────────────────────────────────────┤
│  Container2WASM Conversion              │
│  - CPU emulation (Bochs/TinyEMU)        │
│  - Linux kernel + runc                  │
│  - Container layers → WASM module       │
└─────────────────────────────────────────┘
```

### Limitations

1. **Networking**: Experimental in wasmtime, may have compatibility issues
2. **Performance**: CPU emulation adds overhead (~30-50% slower)
3. **File I/O**: WASI filesystem may have permission constraints
4. **Hardware access**: Limited hardware device support

## 🔗 Resources

- [container2wasm GitHub](https://github.com/container2wasm/container2wasm)
- [wasmtime Documentation](https://docs.wasmtime.dev/)
- [WASI Specification](https://github.com/WebAssembly/WASI)
- [Home Assistant Container](https://www.home-assistant.io/installation/linux#docker-compose)

## 📝 Future Improvements

Potential enhancements for WASM testing:

1. **Browser-based tests**: Run tests directly in browsers using WASM
2. **Parallel conversion**: Convert multiple HA versions simultaneously
3. **WASM caching**: Cache converted WASM files in CI/CD
4. **Performance benchmarks**: Compare WASM vs Docker performance
5. **Cross-platform**: Test on different WASM runtimes (wasmer, wazero, etc.)

## 🤝 Contributing

To improve WASM testing:

1. Test different Home Assistant versions
2. Report conversion failures or runtime issues
3. Optimize wasmtime flags for better performance
4. Contribute to container2wasm project

## 📄 License

This integration uses:
- **container2wasm**: Apache 2.0 (with bundled GPL/LGPL components)
- **wasmtime**: Apache 2.0
- **Home Assistant**: Apache 2.0
