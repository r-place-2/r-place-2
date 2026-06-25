#!/usr/bin/env bash
set -e

echo "=== r/place 2 Tunnel ==="

# Check dependencies
command -v python3 >/dev/null 2>&1 || { echo "Need python3"; exit 1; }

# Check for bore or cloudflared
TUNNEL=""
if command -v cloudflared &>/dev/null; then
  TUNNEL="cloudflared"
elif command -v bore &>/dev/null; then
  TUNNEL="bore"
elif [ ! -f /tmp/bore ]; then
  echo "Downloading bore..."
  curl -sL https://github.com/ekzhang/bore/releases/download/v0.5.2/bore-v0.5.2-x86_64-unknown-linux-musl.tar.gz | tar xz -C /tmp
  chmod +x /tmp/bore
  TUNNEL="/tmp/bore"
fi

# Kill old server
kill $(lsof -t -i:3000) 2>/dev/null || true

# Start server in background
echo "[*] Starting server..."
cd "$(dirname "$0")"
python3 server.py &
SERVER_PID=$!
sleep 2

# Verify server is running
if ! kill -0 $SERVER_PID 2>/dev/null; then
  echo "Server failed to start"
  exit 1
fi
echo "[*] Server running (PID: $SERVER_PID)"

# Start tunnel
echo "[*] Starting tunnel..."
if [ "$TUNNEL" = "cloudflared" ]; then
  $TUNNEL tunnel --url http://localhost:3000 &
  TUNNEL_PID=$!
  sleep 3
  echo "[!] Check cloudflared logs for the tunnel URL"
  echo "    Then open (in browser):"
  echo "    https://r-place-2.github.io/canvas.html?backend=YOUR-TUNNEL.xyz"
else
  $TUNNEL local 3000 --to bore.pub &
  TUNNEL_PID=$!
  sleep 2
  # Try to extract the bore URL from the output
  echo "[!] Look for 'listening on bore.pub:XXXXX' above"
  echo "    Then open (in browser):"
  echo "    https://r-place-2.github.io/canvas.html?backend=bore.pub:XXXXX&protocol=ws"
fi

cleanup() {
  kill $TUNNEL_PID 2>/dev/null
  kill $SERVER_PID 2>/dev/null
}
trap cleanup EXIT

wait
