#!/bin/bash
# Run the web server and call cloudflared, like the old project's run.sh:
# the server in the background, a Cloudflare quick tunnel in the foreground
# printing the https://<words>.trycloudflare.com URL for the phone.
#
#   ./run.sh                 # server on 8080 (PORT=8098 ./run.sh to move it)
#   ./run.sh --simulate      # the same, with fake hardware
#
# The systemd unit arducam-ptz-web may already hold port 8080 on this machine;
# systemctl --user stop arducam-ptz-web first, or pick PORT.
cd "$(dirname "$0")"
source ./.venv/bin/activate

PORT="${PORT:-8080}"
arducam serve --port "$PORT" "$@" &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null' EXIT
cloudflared tunnel --url "http://localhost:$PORT"