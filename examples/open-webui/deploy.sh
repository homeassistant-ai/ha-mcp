#!/bin/bash
# Deploy MCP Testing Stack to a remote machine
#
# Usage:
#   ./deploy.sh <remote_host> [frontend]
#
# Examples:
#   ./deploy.sh user@server.example.com            # Deploy Open WebUI stack
#   ./deploy.sh user@server.example.com librechat  # Deploy LibreChat stack
#
# Prerequisites:
#   - SSH access to the remote machine
#   - Docker and Docker Compose installed on remote
#   - .env file configured locally (will be copied)

set -e

REMOTE_HOST="${1:-}"
FRONTEND="${2:-openwebui}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REMOTE_DIR="~/mcp-test"

if [[ -z "$REMOTE_HOST" ]]; then
    echo "Usage: $0 <remote_host> [frontend]"
    echo ""
    echo "Frontends: openwebui (default), librechat"
    echo ""
    echo "Example:"
    echo "  $0 user@my-server.com"
    echo "  $0 user@my-server.com librechat"
    exit 1
fi

# Check if .env exists
if [[ ! -f "$SCRIPT_DIR/.env" ]]; then
    echo "Error: .env file not found"
    echo "Copy .env.example to .env and configure it first:"
    echo "  cp .env.example .env"
    exit 1
fi

echo "=== Deploying MCP Testing Stack to $REMOTE_HOST ==="
echo "Frontend: $FRONTEND"
echo ""

# Create remote directory
echo "Creating remote directory..."
ssh "$REMOTE_HOST" "mkdir -p $REMOTE_DIR"

# Copy files
echo "Copying files..."
scp "$SCRIPT_DIR/.env" "$REMOTE_HOST:$REMOTE_DIR/"
scp "$SCRIPT_DIR/mini_mcp.py" "$REMOTE_HOST:$REMOTE_DIR/"

if [[ "$FRONTEND" == "librechat" ]]; then
    scp "$SCRIPT_DIR/docker-compose.librechat.yml" "$REMOTE_HOST:$REMOTE_DIR/docker-compose.yml"
    scp "$SCRIPT_DIR/librechat.yaml" "$REMOTE_HOST:$REMOTE_DIR/"
else
    scp "$SCRIPT_DIR/docker-compose.yml" "$REMOTE_HOST:$REMOTE_DIR/"
fi

# Start containers
echo "Starting containers..."
ssh "$REMOTE_HOST" "cd $REMOTE_DIR && docker compose pull && docker compose up -d"

echo ""
echo "=== Deployment complete ==="
echo ""
echo "Wait for model download, then access:"
if [[ "$FRONTEND" == "librechat" ]]; then
    echo "  LibreChat:     http://$REMOTE_HOST:3080"
else
    echo "  Open WebUI:    http://$REMOTE_HOST:3000"
fi
echo "  MCPO mini-mcp: http://$REMOTE_HOST:8001/mini-mcp/docs"
echo "  MCPO ha-mcp:   http://$REMOTE_HOST:8000/ha-mcp/docs"
echo ""
echo "Monitor logs:"
echo "  ssh $REMOTE_HOST 'cd $REMOTE_DIR && docker compose logs -f'"
echo ""
echo "Test prompts:"
echo "  1. 'What time is it?' (tests mini-mcp get_time)"
echo "  2. 'Add 5 and 7' (tests mini-mcp add_numbers)"
echo "  3. 'Say hello to Bob' (tests mini-mcp greet)"
echo ""
echo "If mini-mcp works, add ha-mcp in Open WebUI settings:"
echo "  Admin Panel -> Settings -> Tools -> Add Connection"
echo "  URL: http://mcpo-ha:8000/ha-mcp (from inside container)"
