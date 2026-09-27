"""
Untethered Beacon: Fleet UDP Presence Announcement & Push Notifications
"""
import json
import select
import socket
import time


def _background_daemon(ota_interval, enable_telnet=True):
    # Setup Telnet server socket if enabled
    telnet_sock = None
    if enable_telnet:
        try:
            telnet_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            telnet_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            telnet_sock.setblocking(False)
            telnet_sock.bind(("0.0.0.0", _telnet_port))
            telnet_sock.listen(1)
            print(f"[Untethered] Remote REPL active on port {_telnet_port}")
        except Exception as e:
            print(f"[Untethered] Could not start Telnet server: {e}")
            telnet_sock = None
    else:
        print("[Untethered] Remote REPL (Telnet) is disabled.")

    # Setup Beacon socket
    beacon_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    beacon_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    beacon_sock.setblocking(False)
    try:
        beacon_sock.bind(("0.0.0.0", _beacon_port))
    except Exception:
        pass

    broadcast_addr = ("255.255.255.255", _beacon_port)
    beacon_timer = 0
    ota_timer = 0
    wifi_timer = 0
    wifi_backoff = 10  # seconds; doubles while the network stays down (max 5 min)
    pending_login = None
    login_cooldown = 0  # ticks; slows password guessing after a failed login

    while True:
        # Feed hardware watchdog if enabled
        if _wdt:
            _wdt.feed()

        # 1. Handle Telnet logins and incoming connections. One login is collected at a time,
        # a little per tick, so an idle or hostile client can never stall the other services.
        # A logged-in session is only replaced once the new client has authenticated.
        if telnet_sock:
            try:
                if pending_login:
                    verdict = pending_login.poll(_telnet_password)
                    if verdict is not None:
                        client_sock = pending_login.sock
                        pending_login = None
                        if verdict:
                            _open_telnet_session(client_sock)
                        else:
                            client_sock.close()
                            login_cooldown = 10
                elif login_cooldown:
                    login_cooldown -= 1
                else:
                    r, _, _ = select.select([telnet_sock], [], [], 0)
                    if r:
                        client_sock, client_addr = telnet_sock.accept()
                        print(f"[Untethered] Telnet client connected from {client_addr}")
                        if _telnet_password:
                            pending_login = _TelnetLogin(client_sock)
                        else:
                            _open_telnet_session(client_sock)
            except Exception:
                if pending_login:
                    try:
                        pending_login.sock.close()
                    except Exception:
                        pass
                    pending_login = None

        # 2. Handle incoming UDP push commands & presence beacon
        try:
            r, _, _ = select.select([beacon_sock], [], [], 0)
            if r:
                data, addr = beacon_sock.recvfrom(512)
                msg = json.loads(data.decode("utf-8"))
                target = msg.get("target", "all")
                if target in ("all", _device_name):
                    if msg.get("cmd") == "ota":
                        _handle_push(msg)
                    elif msg.get("cmd") == "wipe":
                        _handle_remote_wipe(msg, target)
        except Exception:
            pass

        # Broadcast beacon every _beacon_interval seconds (ticks are 100ms)
        beacon_timer += 1
        if beacon_timer >= _beacon_interval * 10:
            beacon_timer = 0
            state = _get_local_state()
            comp = state.get("components", {})
            payload = {
                "id": _device_name,
                "ip": get_ip(),
                "version": state.get("version", "0.0.0"),
                "app_hash": comp.get("app", "")[:7],
                "sys_hash": comp.get("system", "")[:7],
                "status": _device_status
            }
            try:
                beacon_sock.sendto(json.dumps(payload).encode("utf-8"), broadcast_addr)
            except Exception:
                pass

        # Keep the board reachable: reconnect Wi-Fi if the link dropped
        wifi_timer += 1
        if wifi_timer >= wifi_backoff * 10:
            wifi_timer = 0
            wifi_backoff = 10 if _reconnect_wifi() else min(wifi_backoff * 2, 300)

        # Periodic OTA poll if configured
        if ota_interval and ota_interval > 0:
            ota_timer += 1
            if ota_timer >= (ota_interval * 10):
                ota_timer = 0
                try:
                    check_update()
                except Exception as e:
                    print(f"[Untethered] Periodic OTA check error: {e}")

        time.sleep_ms(100)


def _handle_push(msg):
    """
    A push only says "check now". Its URL is honoured when manifests are signed (the signature
    protects the content), or when no manifest URL is configured (open dev mode).
    Otherwise the board checks its own configured manifest URL.
    """
    push_url = msg.get("url")
    if not push_url or (not _secret_key and (_manifest_url or not _allow_unsigned_push)):
        push_url = _manifest_url
    if not push_url:
        print("[Untethered] Push ignored: unsigned board without OTA_MANIFEST_URL. "
              "Set OTA_SECRET_KEY, or OTA_ALLOW_UNSIGNED_PUSH = True on a trusted LAN.")
        return
    print(f"[Untethered] Received instant push trigger! Updating from {push_url}...")
    check_update(push_url)


def _handle_remote_wipe(msg, target):
    """Remote wipe requires OTA_SECRET_KEY and a fresh, signed sequence number."""
    scope = msg.get("scope", "app")
    seq = msg.get("seq")
    if scope not in ("app", "system", "all"):
        return
    if not _secret_key:
        print("[Untethered] Remote wipe ignored: no OTA_SECRET_KEY configured.")
        return
    expected = _compute_hmac_sha256(_secret_key, f"wipe:{scope}:{target}:{seq}")
    if not _constant_time_compare(msg.get("signature"), expected):
        print("[Untethered] SECURITY ERROR: Wipe rejected - signature invalid.")
        return
    if not _seq_is_fresh("wipe", seq):
        print("[Untethered] SECURITY ERROR: Wipe rejected - replayed command.")
        return
    _save_seq("wipe", seq)
    print(f"[Untethered] Received remote wipe command (scope: {scope}). Executing...")
    wipe(scope=scope, reboot=True)
