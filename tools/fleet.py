"""
Fleet Monitor & Discovery CLI (tools/fleet.py)
Listens for UDP heartbeats and renders a real-time table of all active RP2040-W boards.
The table adapts to the terminal: columns size to their contents, the least important ones
are dropped when the window is narrow, and rows beyond the window height are summarised.
"""

import argparse
import json
import os
import shutil
import socket
import sys
import time

DEFAULT_BEACON_PORT = 8266

# (key, header, max width); the order is the display order
COLUMNS = [
    ("id", "DEVICE ID", 24),
    ("group", "GROUP", 16),
    ("ip", "IP ADDRESS", 15),
    ("version", "VERSION", 20),
    ("app_hash", "APP", 7),
    ("sys_hash", "SYS", 7),
    ("status", "STATUS", 11),
    ("seen", "SEEN", 9),
]
# Dropped first to last when the table is wider than the terminal
DROP_ORDER = ["sys_hash", "app_hash", "group", "version", "ip"]
ID_SOFT_MIN = 14  # Long device IDs are shortened to this before any column is dropped
MIN_ID_WIDTH = 8
GAP = "  "
TITLE = "UNTETHERED FLEET MONITOR"


def _fit(text, width):
    return text if len(text) <= width else text[:max(width - 1, 0)] + "~"


def build_rows(devices, now, offline_after):
    rows = []
    for dev_id, dev in sorted(devices.items()):
        elapsed = int(now - dev["last_seen"])
        rows.append({
            "id": dev_id,
            "group": dev["group"],
            "ip": dev["ip"],
            "version": dev["version"],
            "app_hash": dev["app_hash"],
            "sys_hash": dev["sys_hash"],
            "status": "OFFLINE" if elapsed > offline_after else dev["status"],
            "seen": f"{elapsed}s ago",
        })
    return rows


def layout(rows, width):
    """Returns [(key, header, col_width)] that fits in width, dropping columns in DROP_ORDER."""
    cols = []
    for key, header, cap in COLUMNS:
        values = [r[key] for r in rows]
        if key == "group" and all(v == "-" for v in values):
            continue  # No board has a group: the column would only show dashes
        cols.append((key, header, min(max([len(header)] + [len(v) for v in values]), cap)))

    def total(cs):
        return sum(w for _, _, w in cs) + len(GAP) * (len(cs) - 1)

    def with_id_width(cs, floor):
        # The ID column takes whatever the others leave, between floor and its natural width
        key, header, natural = cols[0]
        spare = width - (total(cs) - cs[0][2])
        return [(key, header, max(min(floor, natural), min(natural, spare)))] + cs[1:]

    # Shorten long device IDs a little before giving up whole columns, then drop columns
    fitted = with_id_width(cols, ID_SOFT_MIN)
    for key in DROP_ORDER:
        if total(fitted) <= width:
            break
        fitted = with_id_width([c for c in fitted if c[0] != key], ID_SOFT_MIN)
    return with_id_width(fitted, MIN_ID_WIDTH)


def render(devices, now, offline_after, width, height, port):
    """Builds the dashboard as a list of lines no wider than width and no taller than height."""
    rows = build_rows(devices, now, offline_after)
    cols = layout(rows, width)
    table_width = min(width, sum(w for _, _, w in cols) + len(GAP) * (len(cols) - 1))
    rule_width = max(table_width, min(width, len(TITLE) + 4))

    online = sum(1 for r in rows if r["status"] != "OFFLINE")
    summary = f"{online}/{len(rows)} online | UDP {port}"
    title = TITLE if len(TITLE) + len(summary) + 2 > rule_width else \
        TITLE + summary.rjust(rule_width - len(TITLE))

    def line(values):
        return GAP.join(_fit(v, w).ljust(w) for v, (_, _, w) in zip(values, cols)).rstrip()

    out = ["=" * rule_width, _fit(title, width), "=" * rule_width,
           line([h for _, h, _ in cols]), "-" * rule_width]
    footer = ["-" * rule_width, _fit("Ctrl+C to quit.", width)]
    if len(out) + len(rows) + len(footer) > height:
        # Short window: give the frame's lines to devices
        out = [_fit(title, width), line([h for _, h, _ in cols]), "-" * rule_width]
        footer = []

    if not rows:
        out.append(_fit("No devices detected yet. Awaiting heartbeats...", width))
    else:
        room = max(height - len(out) - len(footer), 1)
        shown = rows if len(rows) <= room else rows[:room - 1]
        out += [line([r[k] for k, _, _ in cols]) for r in shown]
        if len(shown) < len(rows):
            out.append(_fit(f"... and {len(rows) - len(shown)} more (enlarge the window to see all)", width))
    return [_fit(l, width) for l in out + footer][:max(height, 1)]  # Last resort for tiny windows


def _enable_ansi():
    """Turns on escape-code handling in the Windows console (a no-op elsewhere)."""
    if os.name == "nt":
        os.system("")


def draw(lines):
    if sys.stdout.isatty():
        # Home the cursor and overwrite in place (no full clear, so no flicker); \033[K clears
        # leftovers of longer old lines and \033[J anything below after the window shrinks
        sys.stdout.write("\033[H" + "".join(l + "\033[K\n" for l in lines) + "\033[J")
    else:
        sys.stdout.write("\n".join(lines) + "\n\n")
    sys.stdout.flush()


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
    _enable_ansi()
    if sys.stdout.isatty():
        sys.stdout.write("\033[2J")

    try:
        while True:
            try:
                data, addr = sock.recvfrom(1024)
                info = json.loads(data.decode("utf-8"))
                if "cmd" in info or "id" not in info:
                    continue  # push/wipe commands from deploy.py share this port; not heartbeats
                devices[str(info["id"])] = {
                    "ip": str(info.get("ip") or addr[0]),
                    "group": str(info.get("group") or "-"),
                    "version": str(info.get("version") or "unknown"),
                    "app_hash": str(info.get("app_hash") or "-"),
                    "sys_hash": str(info.get("sys_hash") or "-"),
                    "status": str(info.get("status") or "ONLINE"),
                    "last_seen": time.time(),
                }
            except socket.timeout:
                pass
            except Exception:
                pass

            # Drain every queued heartbeat first; redraw at most once per second (which also
            # picks up window resizes)
            now = time.time()
            if now - last_render < 1:
                continue
            last_render = now

            size = shutil.get_terminal_size((100, 30))
            # One column spare: writing into the last column makes some terminals wrap
            draw(render(devices, now, args.offline_after, size.columns - 1, size.lines - 1, args.port))

    except KeyboardInterrupt:
        print("\nExiting fleet monitor.")
        sock.close()


if __name__ == "__main__":
    main()
