#!/usr/bin/env zsh
# ══════════════════════════════════════════════════════════════
#  install_launchd.sh — Install/uninstall macOS launchd service
# ══════════════════════════════════════════════════════════════
#
#  Usage:
#    ./scripts/install_launchd.sh install    Install & load
#    ./scripts/install_launchd.sh uninstall  Unload & remove
#    ./scripts/install_launchd.sh status     Check if loaded
#
# ══════════════════════════════════════════════════════════════

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.trading.0dte-engine"
PLIST_PATH="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG_DIR="${PROJECT_DIR}/data/logs"

install_service() {
    mkdir -p "$HOME/Library/LaunchAgents"
    mkdir -p "$LOG_DIR"

    cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>

    <key>ProgramArguments</key>
    <array>
        <string>${PROJECT_DIR}/scripts/supervised_start.sh</string>
    </array>

    <key>WorkingDirectory</key>
    <string>${PROJECT_DIR}</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>${PROJECT_DIR}/.venv/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>HOME</key>
        <string>${HOME}</string>
    </dict>

    <!-- Run on load (login) -->
    <key>RunAtLoad</key>
    <true/>

    <!-- Restart if it exits with non-zero -->
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>

    <!-- Throttle restarts (min 30s between) -->
    <key>ThrottleInterval</key>
    <integer>30</integer>

    <!-- Logs -->
    <key>StandardOutPath</key>
    <string>${LOG_DIR}/launchd_stdout.log</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/launchd_stderr.log</string>
</dict>
</plist>
EOF

    echo "📄 Plist written to: $PLIST_PATH"

    # Load the service
    launchctl load "$PLIST_PATH"
    echo "✅ Service loaded. It will:"
    echo "   • Start on login"
    echo "   • Restart automatically on crash"
    echo ""
    echo "   To start now:   launchctl start ${LABEL}"
    echo "   To stop:        launchctl stop ${LABEL}"
    echo "   To uninstall:   $0 uninstall"
}

uninstall_service() {
    if [ -f "$PLIST_PATH" ]; then
        launchctl unload "$PLIST_PATH" 2>/dev/null || true
        rm -f "$PLIST_PATH"
        echo "✅ Service unloaded and plist removed."
    else
        echo "ℹ️  No service installed (plist not found)."
    fi
}

check_status() {
    if launchctl list | grep -q "$LABEL"; then
        echo "✅ Service is loaded"
        launchctl list "$LABEL" 2>/dev/null || true
    else
        echo "⏹  Service is not loaded"
    fi
}

case "${1:-}" in
    install)   install_service ;;
    uninstall) uninstall_service ;;
    status)    check_status ;;
    *)
        echo "Usage: $0 {install|uninstall|status}"
        exit 1
        ;;
esac
