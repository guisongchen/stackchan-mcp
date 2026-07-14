#!/usr/bin/env bash
# Start/stop/status helper for the StackChan MCP Streamable HTTP daemon.
#
# This daemon is used by the custom fork that relies on the local ASR Core
# (/tmp/asr_core.sock) and TTS Core (/tmp/tts_core.sock) services. It exposes
# the MCP endpoint at http://127.0.0.1:8767/mcp and the ESP32 WebSocket
# endpoint on port 8765.
#
# Usage:
#   stackchan-mcp-daemon.sh start    # start in background
#   stackchan-mcp-daemon.sh stop     # stop the daemon
#   stackchan-mcp-daemon.sh status   # print pid / health
#   stackchan-mcp-daemon.sh restart  # stop then start

set -euo pipefail

PIDFILE="${XDG_RUNTIME_DIR:-/tmp}/stackchan-mcp-daemon.pid"
LOGFILE="${XDG_RUNTIME_DIR:-/tmp}/stackchan-mcp-daemon.log"
HOST="${MCP_HTTP_HOST:-127.0.0.1}"
PORT="${MCP_HTTP_PORT:-8767}"
HEALTH_URL="http://${HOST}:${PORT}/healthz"

start() {
    if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        echo "stackchan-mcp daemon already running (pid $(cat "$PIDFILE"))"
        return 0
    fi

    # Make sure we do not use a SOCKS/HTTP proxy for localhost Unix sockets.
    unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY all_proxy ALL_PROXY

    rm -f "$PIDFILE"
    nohup stackchan-mcp serve --transport streamable-http --no-mdns \
        >"$LOGFILE" 2>&1 &
    echo $! >"$PIDFILE"

    # Wait for the health endpoint to become available.
    for _ in {1..30}; do
        if curl -fs "$HEALTH_URL" >/dev/null 2>&1; then
            echo "stackchan-mcp daemon started (pid $(cat "$PIDFILE"))"
            return 0
        fi
        sleep 0.2
    done

    echo "stackchan-mcp daemon failed to start; see $LOGFILE"
    return 1
}

stop() {
    if [[ -f "$PIDFILE" ]]; then
        pid="$(cat "$PIDFILE")"
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
            # Give it a moment to release the ownership lock.
            for _ in {1..20}; do
                kill -0 "$pid" 2>/dev/null || break
                sleep 0.2
            done
            kill -9 "$pid" 2>/dev/null || true
            echo "stackchan-mcp daemon stopped"
        else
            echo "stackchan-mcp daemon not running"
        fi
        rm -f "$PIDFILE"
    else
        echo "stackchan-mcp daemon not running (no pidfile)"
    fi
}

status() {
    unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY all_proxy ALL_PROXY
    if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        echo "stackchan-mcp daemon running (pid $(cat "$PIDFILE"))"
        curl -fs "$HEALTH_URL" 2>/dev/null && echo "health: ok" || echo "health: unreachable"
    else
        echo "stackchan-mcp daemon not running"
        return 1
    fi
}

case "${1:-}" in
    start)
        start
        ;;
    stop)
        stop
        ;;
    restart)
        stop
        start
        ;;
    status)
        status
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status}"
        exit 1
        ;;
esac
