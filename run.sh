#!/usr/bin/env bash
# Launch (or restart) the Polar H10 Shot Monitor dashboard.
# Usage:  ./run.sh

PORT=8050
DIR="$(cd "$(dirname "$0")" && pwd)"

# Kill any existing dashboard on the port
PID=$(lsof -ti tcp:"$PORT" 2>/dev/null)
if [ -n "$PID" ]; then
    echo "Stopping existing dashboard (PID $PID)..."
    kill "$PID" 2>/dev/null
    sleep 2
fi

# Disconnect any lingering BLE connections (Polar H10 devices)
if command -v bluetoothctl &>/dev/null; then
    echo "Clearing stale BLE connections..."
    # Get list of connected devices, disconnect any Polar ones
    bluetoothctl devices Connected 2>/dev/null | while read -r _ addr name; do
        if echo "$name" | grep -qi polar; then
            echo "  Disconnecting $name ($addr)..."
            bluetoothctl disconnect "$addr" 2>/dev/null
        fi
    done
    # Also power-cycle the adapter to fully reset BLE state
    bluetoothctl power off 2>/dev/null
    sleep 1
    bluetoothctl power on 2>/dev/null
    sleep 1
    echo "BLE adapter reset."
fi

echo "Starting dashboard at http://localhost:$PORT"
cd "$DIR" && python -m src.dashboard_app
