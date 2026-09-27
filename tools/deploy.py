"""
Host PC Deployment CLI & Server for RP2040-W Fleet (tools/deploy.py)
- Recursively scans project directories into distinct components ('app' vs 'system')
- Supports --project-dir to deploy external projects at their natural filesystem paths
- Places lib/ under 'system' and project application files under 'app'
- Computes per-file SHA-256 and deterministic composite component hashes
- Generates manifest.json with component-level hashing and HMAC-SHA256 signatures
- Hosts an HTTP distribution server that only serves files listed in the manifest
- Sends UDP push notifications to wake up and flash boards instantly
"""

import argparse
import hashlib
import hmac
import http.server
import ipaddress
import json
import os
import re
import socket
import socketserver
import sys
import threading
import time
import urllib.parse

DEFAULT_PORT = 8000
DEFAULT_BEACON_PORT = 8266

EXCLUDED_EXTENSIONS = [".pyc", ".tmp", ".ota_new", ".bak"]
EXCLUDED_NAMES = ["__pycache__", ".git", ".gemini", ".idea", ".vscode", "tools", "scratch"]
# Never shipped to devices: per-device identity/secrets and device-side state files.
# Matched against the project-root path only, so e.g. app/config.py still deploys.
EXCLUDED_FILES = ["config.py", "config.example.py", "version.json", "manifest.json",
                  ".untethered_seq.json", ".ota_journal.json"]
# Last sequence number issued by this host, so seq never goes backwards with the clock
SEQ_STATE_FILE = ".untethered_host_seq"


def next_seq(state_dir):
    """Monotonic sequence number for signed commands; devices reject anything older (anti-replay)."""
    path = os.path.join(state_dir, SEQ_STATE_FILE)
    try:
        with open(path) as f:
            last = int(f.read().strip())
    except (OSError, ValueError):
        last = 0
    seq = max(int(time.time()), last + 1)
    with open(path, "w") as f:
        f.write(str(seq))
    return seq


def compute_signature(secret, version, app_hash, sys_hash, seq, targets=None):
    """
    Computes standard RFC 2104 HMAC-SHA256 signature over version:app_hash:sys_hash:seq, plus
    :target1,target2 for targeted manifests (must match _signed_payload on the board).
    """
    payload = f"{version}:{app_hash}:{sys_hash}:{seq}"
    if targets:
        payload += ":" + ",".join(targets)
    payload = payload.encode("utf-8")
    key = secret.encode("utf-8") if isinstance(secret, str) else secret
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def compute_wipe_signature(secret, scope, target, seq):
    """Computes standard RFC 2104 HMAC-SHA256 signature for remote wipe commands."""
    payload = f"wipe:{scope}:{target}:{seq}".encode("utf-8")
    key = secret.encode("utf-8") if isinstance(secret, str) else secret
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def is_lan_ip(ip):
    """Private IPv4 (10/8, 172.16/12, 192.168/16). Excludes loopback, link-local and the
    100.64/10 carrier-grade NAT range that Tailscale uses."""
    try:
        addr = ipaddress.IPv4Address(ip)
    except ValueError:
        return False
    return addr.is_private and not addr.is_loopback and not addr.is_link_local


def _default_route_ip():
    """Address of the interface that carries the default route (connect() on UDP sends nothing)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 1))
        return s.getsockname()[0]
    except Exception:
        return None
    finally:
        s.close()


def lan_ip_candidates():
    """Every LAN address of this machine, the default-route interface first."""
    candidates = [_default_route_ip()]
    try:
        candidates += socket.gethostbyname_ex(socket.gethostname())[2]
    except Exception:
        pass
    result = []
    for ip in candidates:
        if ip and is_lan_ip(ip) and ip not in result:
            result.append(ip)
    return result


def get_local_ip(override_ip=None):
    """
    Detects this machine's LAN address. The default-route interface is preferred over the
    first address the hostname resolves to, which on Windows is often a Hyper-V, WSL or
    Docker adapter that the boards cannot reach.
    """
    if override_ip:
        return override_ip
    candidates = lan_ip_candidates()
    return candidates[0] if candidates else (_default_route_ip() or "127.0.0.1")


def broadcast(payload, beacon_port, host_ip=None, repeat=3):
    """
    Broadcasts payload to the boards. The socket is bound to host_ip so the limited broadcast
    leaves through the interface the boards are on (on Windows it otherwise picks one adapter),
    and the /24 subnet broadcast is sent as well for hosts where binding does not steer it.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    targets = ["255.255.255.255"]
    if host_ip and is_lan_ip(host_ip):
        try:
            sock.bind((host_ip, 0))
        except OSError:
            pass
        targets.append(str(ipaddress.IPv4Network(host_ip + "/24", strict=False).broadcast_address))
    try:
        for _ in range(repeat):
            for addr in targets:
                try:
                    sock.sendto(payload, (addr, beacon_port))
                except OSError:
                    pass
            time.sleep(0.1)
    finally:
        sock.close()


def compute_sha256(filepath):
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while True:
            chunk = f.read(4096)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def compute_composite_hash(file_entries):
    """
    Computes a deterministic composite SHA-256 hash over a list of file entries.
    Each entry must contain 'path' and 'sha256'.
    """
    sorted_entries = sorted(file_entries, key=lambda x: x["path"])
    h = hashlib.sha256()
    for entry in sorted_entries:
        line = f"{entry['path']}:{entry['sha256']}\n".encode("utf-8")
        h.update(line)
    return h.hexdigest()


def scan_dir_recursive(base_dir):
    """Recursively collects relative paths of deployable files in a directory."""
    files = []
    if not os.path.isdir(base_dir):
        return files
    for root, dirs, filenames in os.walk(base_dir):
        dirs[:] = [d for d in dirs if d not in EXCLUDED_NAMES]
        for filename in filenames:
            ext = os.path.splitext(filename)[1]
            if ext in EXCLUDED_EXTENSIONS:
                continue
            rel_path = os.path.relpath(os.path.join(root, filename), base_dir)
            files.append(rel_path.replace("\\", "/"))
    return sorted(files)


def find_library(root_dir):
    """Locates the bundled runtime produced by tools/bundle.py (prefers precompiled .mpy)."""
    for name in ("untethered.mpy", "untethered.py"):
        path = os.path.join(root_dir, "dist", name)
        if os.path.isfile(path):
            return f"lib/{name}", path
    return None, None


def parse_targets(value):
    """
    --target value -> manifest target list: None for 'all' (untargeted), else sorted unique
    device names and groups. Untargeted manifests only reach boards without a DEVICE_GROUP.
    """
    names = sorted({t.strip() for t in (value or "").split(",") if t.strip()})
    if not names or names == ["all"]:
        return None
    if "all" in names:
        raise ValueError("'all' cannot be combined with other targets")
    return names


def manifest_filename(targets):
    """Separate files per target list, so deploys for different groups can run side by side
    (on different --port values) without overwriting each other's manifest."""
    if not targets:
        return "manifest.json"
    return "manifest." + re.sub(r"[^A-Za-z0-9._+-]", "_", "+".join(targets)) + ".json"


def build_manifest(root_dir, project_dir, host_ip, port, version, secret=None, include_boot=False,
                   targets=None):
    """
    Builds manifest.json. Returns (manifest, served_files) where served_files maps each
    manifest path to the local file the HTTP server is allowed to serve for it.
    boot.py is left out unless include_boot is set (boards also need OTA_ALLOW_BOOT_UPDATE).
    targets (device names and groups, from parse_targets) limits which boards install it.
    """
    if not project_dir or not os.path.isdir(project_dir):
        raise FileNotFoundError(f"Project directory not found: {project_dir}")

    manifest = {
        "version": version,
        "seq": next_seq(root_dir),
        **({"targets": list(targets)} if targets else {}),
        "components": {
            "app": {
                "hash": "",
                "files": []
            },
            "system": {
                "hash": "",
                "files": []
            }
        }
    }
    served_files = {}

    def add_file(rel, full_path):
        entry = {
            "path": rel,
            "url": f"http://{host_ip}:{port}/{urllib.parse.quote(rel)}",
            "sha256": compute_sha256(full_path)
        }
        # Files in lib/ belong to system component; everything else belongs to app
        component = "system" if rel.startswith("lib/") else "app"
        manifest["components"][component]["files"].append(entry)
        served_files[rel] = full_path

    for rel in scan_dir_recursive(project_dir):
        # Skip configs and device state so individual device identities are preserved
        if rel in EXCLUDED_FILES:
            continue
        if rel == "boot.py" and not include_boot:
            print("[Info] Skipping boot.py (it keeps boards reachable). Use --include-boot to deploy it.")
            continue
        add_file(rel, os.path.join(project_dir, rel))

    # Ensure the untethered runtime is always part of the system component
    if not any(p in served_files for p in ("lib/untethered.mpy", "lib/untethered.py")):
        rel, path = find_library(root_dir)
        if rel:
            add_file(rel, path)
        else:
            print("[Warning] dist/untethered.mpy not found. Run tools/bundle.py first.")

    manifest["components"]["app"]["hash"] = compute_composite_hash(manifest["components"]["app"]["files"])
    manifest["components"]["system"]["hash"] = compute_composite_hash(manifest["components"]["system"]["files"])

    if secret:
        manifest["signature"] = compute_signature(
            secret,
            version,
            manifest["components"]["app"]["hash"],
            manifest["components"]["system"]["hash"],
            manifest["seq"],
            targets,
        )

    manifest_path = os.path.join(root_dir, manifest_filename(targets))
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)
    served_files["manifest.json"] = manifest_path

    return manifest, served_files


class ManifestHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
    """
    Serves exactly the files listed in the manifest (plus manifest.json) and nothing else,
    so host secrets such as config.py can never be fetched and paths cannot escape.
    """
    served_files = {}

    def _resolve(self):
        clean_path = self.path.split("?", 1)[0].split("#", 1)[0]
        return self.served_files.get(urllib.parse.unquote(clean_path).lstrip("/"))

    def translate_path(self, path):
        return self._resolve() or ""

    def do_GET(self):
        if not self._resolve():
            self.send_error(404, "Not in manifest")
            return
        super().do_GET()

    def do_HEAD(self):
        if not self._resolve():
            self.send_error(404, "Not in manifest")
            return
        super().do_HEAD()

    def log_message(self, format, *args):
        sys.stderr.write(f"[HTTP] {self.address_string()} - {format % args}\n")


def send_push_beacon(beacon_port, target, manifest_url, host_ip=None):
    """Sends a UDP broadcast to notify devices to update immediately."""
    payload = json.dumps({
        "cmd": "ota",
        "target": target,
        "url": manifest_url
    }).encode("utf-8")

    label = ", ".join(target) if isinstance(target, list) else target
    print(f"\n[Push] Broadcasting instant OTA trigger to '{label}' on UDP port {beacon_port}...")
    broadcast(payload, beacon_port, host_ip)


def send_wipe_beacon(beacon_port, target, scope, secret, state_dir, host_ip=None):
    """Sends a UDP broadcast commanding target devices to wipe the specified scope."""
    seq = next_seq(state_dir)
    msg = {
        "cmd": "wipe",
        "scope": scope,
        "target": target,
        "seq": seq,
        "signature": compute_wipe_signature(secret, scope, target, seq),
    }

    payload = json.dumps(msg).encode("utf-8")
    print(f"\n[Push] Broadcasting remote WIPE command (scope='{scope}', target='{target}') on UDP port {beacon_port}...")
    broadcast(payload, beacon_port, host_ip)


def main():
    parser = argparse.ArgumentParser(description="RP2040-W Component-Aware OTA Deploy Tool")
    parser.add_argument("--project-dir", type=str, default=None,
                        help="Path to an external project directory containing its own boot.py, main.py, etc.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="HTTP server port (default 8000)")
    parser.add_argument("--version", type=str, default=None,
                        help="Firmware version string (default: a dev-YYYYMMDD-HHMMSS timestamp)")
    parser.add_argument("--target", type=str, default="all",
                        help="Device names and/or DEVICE_GROUPs, comma-separated (e.g. 'radar' or "
                             "'radar-1,radar-2'). Only those boards install the update. Default 'all' "
                             "reaches boards without a group. --wipe takes a single name or group.")
    parser.add_argument("--beacon-port", type=int, default=DEFAULT_BEACON_PORT, help="UDP discovery port")
    parser.add_argument("--host-ip", type=str, default=None, help="Explicit host IP address for devices to reach")
    parser.add_argument("--secret", type=str, default=None,
                        help="Secret key for signing firmware manifest (HMAC-SHA256). Defaults to config.OTA_SECRET_KEY or UNTETHERED_SECRET env var.")
    parser.add_argument("--wipe", choices=["app", "system", "all"], default=None,
                        help="Remotely wipe firmware scope ('app', 'system', or 'all') and reboot into standby/clean mode")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="Skip interactive confirmation prompt when wiping system or all")
    parser.add_argument("--include-boot", action="store_true",
                        help="Also deploy boot.py (boards must set OTA_ALLOW_BOOT_UPDATE = True to accept it)")
    parser.add_argument("--dry-run", action="store_true", help="Generate manifest without running server or push")
    parser.add_argument("--no-push", action="store_true", help="Start server without sending push trigger")

    args = parser.parse_args()
    root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    host_ip = get_local_ip(args.host_ip)
    version = args.version or time.strftime("dev-%Y%m%d-%H%M%S")
    try:
        targets = parse_targets(args.target)
    except ValueError as e:
        print(f"[Error] --target: {e}")
        sys.exit(1)
    if args.wipe and targets and len(targets) > 1:
        print("[Error] --wipe takes a single device name or group (the signature covers one target).")
        sys.exit(1)

    if args.project_dir:
        project_dir = os.path.abspath(args.project_dir)
    elif os.path.exists(os.path.join(os.getcwd(), "main.py")) and os.path.abspath(os.getcwd()) != root_dir:
        project_dir = os.getcwd()
    else:
        project_dir = os.path.join(root_dir, "examples", "simple_project")
    if not args.wipe and not os.path.isdir(project_dir):
        print(f"[Error] Project directory not found: {project_dir}")
        sys.exit(1)

    # Resolve signing secret: --secret flag -> UNTETHERED_SECRET env var -> config.py OTA_SECRET_KEY
    secret = args.secret
    if secret is None:
        secret = os.environ.get("UNTETHERED_SECRET")
    if secret is None:
        try:
            if root_dir not in sys.path:
                sys.path.insert(0, root_dir)
            import config
            secret = getattr(config, "OTA_SECRET_KEY", None)
        except Exception:
            pass

    # Handle remote wipe command if requested
    if args.wipe:
        print("=" * 70)
        print(f" Untethered Remote Wipe Tool | Scope: {args.wipe.upper()}")
        print(f" Target Device: {args.target}")
        print("=" * 70)

        if not secret:
            print("\n[Error] Remote wipe requires a signing secret (--secret or UNTETHERED_SECRET).")
            print("        Devices ignore unsigned wipe commands.")
            sys.exit(1)

        if args.wipe in ("system", "all") and not args.yes:
            warning = "FACTORY RESET: Wiping flash will erase all files including WiFi config!" if args.wipe == "all" else "Wiping system will remove core runtime files!"
            print(f"\n[WARNING] {warning}")
            confirm = input(f"Are you sure you want to wipe '{args.wipe}' on target '{args.target}'? [y/N]: ").strip().lower()
            if confirm != "y":
                print("Wipe operation cancelled by user.")
                return

        send_wipe_beacon(args.beacon_port, args.target, args.wipe, secret=secret, state_dir=root_dir,
                         host_ip=host_ip)
        print(f"\n[Complete] Remote wipe command broadcast successfully for scope '{args.wipe}'.")
        return

    print("=" * 70)
    print(f" Untethered OTA Deployer | Firmware Version: {version}")
    print(f" Repository Root:  {root_dir}")
    print(f" Target Project:   {project_dir}")
    print(f" Manifest URL:     http://{host_ip}:{args.port}/manifest.json  (boards' OTA_MANIFEST_URL)")
    print(f" Targets:          {', '.join(targets) if targets else 'all boards without a DEVICE_GROUP'}")
    others = [ip for ip in lan_ip_candidates() if ip != host_ip] if not args.host_ip else []
    if others:
        print(f" Other LAN addresses: {', '.join(others)}  (pick the boards' network with --host-ip)")
    if secret:
        print(" Cryptographic Signature: HMAC-SHA256 Enabled")
    else:
        print(" Cryptographic Signature: None (Unsigned)")
        print(" Unsigned boards only follow this push if their OTA_MANIFEST_URL points here")
        print(" or they set OTA_ALLOW_UNSIGNED_PUSH = True.")
    print("=" * 70)

    manifest, served_files = build_manifest(root_dir, project_dir, host_ip, args.port, version,
                                            secret=secret, include_boot=args.include_boot, targets=targets)
    manifest_url = f"http://{host_ip}:{args.port}/manifest.json"

    app_comp = manifest["components"]["app"]
    sys_comp = manifest["components"]["system"]

    print(f"\n[Component: APP] Hash: {app_comp['hash']}")
    print(f"  Files ({len(app_comp['files'])}):")
    for f in app_comp["files"]:
        print(f"    • {f['path']} (SHA: {f['sha256'][:10]}...)")

    print(f"\n[Component: SYSTEM / OTA] Hash: {sys_comp['hash']}")
    print(f"  Files ({len(sys_comp['files'])}):")
    for f in sys_comp["files"]:
        print(f"    • {f['path']} (SHA: {f['sha256'][:10]}...)")

    if "signature" in manifest:
        print(f"\n[Signature] HMAC-SHA256: {manifest['signature']}")

    if args.dry_run:
        print("\n[Dry Run] Completed. manifest.json written. Exiting.")
        return

    # Start HTTP server with remapping handler
    ManifestHTTPRequestHandler.served_files = served_files

    socketserver.TCPServer.allow_reuse_address = True
    httpd = socketserver.TCPServer(("", args.port), ManifestHTTPRequestHandler)

    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()

    print(f"\n[HTTP Server] Serving files at http://{host_ip}:{args.port}/")

    if not args.no_push:
        send_push_beacon(args.beacon_port, targets or "all", manifest_url, host_ip=host_ip)

    print("\n[Listening] HTTP server active. Press Ctrl+C to shut down.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[Shutdown] Stopping server.")
        httpd.shutdown()


if __name__ == "__main__":
    main()
