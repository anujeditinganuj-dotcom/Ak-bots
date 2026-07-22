#!/bin/sh
# Starts the bgutil PO-token HTTP server in the background, waits a moment,
# then prints whether it's actually reachable before starting the bot —
# so "is it working" is answered by the container logs on every boot
# instead of staying a silent guess.
node /opt/bgutil-pot/server/build/main.js > /tmp/bgutil-pot.log 2>&1 &
BGUTIL_PID=$!

sleep 3

if kill -0 "$BGUTIL_PID" 2>/dev/null && wget -q -O- http://127.0.0.1:4416/ping >/dev/null 2>&1; then
    echo "[bgutil-pot] OK — PO token server is up on :4416 (YouTube 'web' client should get the full quality ladder)"
else
    echo "[bgutil-pot] WARNING — PO token server did not come up. Last log lines:"
    tail -n 20 /tmp/bgutil-pot.log 2>/dev/null
    echo "[bgutil-pot] Bot will still run — YouTube just falls back to tv_embedded/android's lower-res formats."
fi

exec python3 bot.py
