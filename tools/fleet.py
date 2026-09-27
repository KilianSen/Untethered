"""
Fleet Monitor & Discovery CLI (tools/fleet.py)
Listens for UDP heartbeats and renders a real-time table of all active RP2040-W boards.
"""

import argparse
import json
import os
import socket
import sys
import time

DEFAULT_BEACON_PORT = 8266


def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")


def main():
    parser = argparse.ArgumentParser(description="RP2040-W Fleet Discovery Monitor")
    parser.add_argument("--port", type=int, default=DEFAULT_BEACON_PORT, help="UDP discovery port")
    parser.add_argument("--offline-after", type=int, default=15,
                        help="Seconds without a heartbeat before a board shows OFFLINE (keep > 2x BEACON_INTERVAL)")
    args = parser.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("", args.port))
    except Exception as e:
        print(f"Error binding to port {args.port}: {e}")
        sys.exit(1)

    sock.settimeout(0.2)
    devices = {}
    last_render = 0

    print(f"[Fleet Monitor] Listening for Pico W beacons on UDP port {args.port}...")
    print("Press Ctrl+C to exit.\n")

    try:
        while True:
            try:
                data, addr = sock.recvfrom(1024)
                info = json.loads(data.decode("utf-8"))
                if "cmd" in info or "id" not in info:
                    continue  # push/wipe commands from deploy.py share this port; not heartbeats
                dev_id = info["id"]
                devices[dev_id] = {
                    "ip": info.get("ip", addr[0]),
                    "version": info.get("version", "unknown"),
                    "app_hash": info.get("app_hash", "-"),
                    "sys_hash": info.get("sys_hash", "-"),
                    "status": info.get("status", "ONLINE"),
                    "last_seen": time.time(),
                }
            except socket.timeout:
                pass
            except Exception:
                pass

            # Drain every queued heartbeat first; redraw at most once per second
            now = time.time()
            if now - last_render < 1:
                continue
            last_render = now

            # Render dashboard
            clear_screen()
            print("=" * 82)
            print("  UNTETHERED ACTIVE FLEET MONITOR")
            print("=" * 82)
            print(f"{'DEVICE ID':<16} {'IP ADDRESS':<16} {'VER':<8} {'APP HASH':<10} {'SYS HASH':<10} {'STATUS':<10} {'SEEN':<8}")
            print("-" * 82)

            if not devices:
                print("  No devices detected yet. Awaiting heartbeats...")
            else:
                for dev_id, dev in sorted(devices.items()):
                    elapsed = int(now - dev["last_seen"])
                    state_str = dev["status"]
                    if elapsed > args.offline_after:
                        state_str = "OFFLINE"
                    print(f"{dev_id:<16} {dev['ip']:<16} {dev['version']:<8} {dev['app_hash']:<10} {dev['sys_hash']:<10} {state_str:<10} {elapsed}s ago")

            print("-" * 82)
            print("Ctrl+C to quit.")

    except KeyboardInterrupt:
        print("\nExiting fleet monitor.")
        sock.close()


if __name__ == "__main__":
    main()
