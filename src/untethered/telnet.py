"""
Untethered Telnet: Wireless REPL (os.dupterm) & Stream Management
"""
import io
import os
import select
import time


class _TelnetStream(io.IOBase):
    def __init__(self, sock):
        self.sock = sock
        self.sock.setblocking(False)

    def readinto(self, buf):
        try:
            n = self.sock.readinto(buf)
            if n is None:
                return None
            if n == 0:
                self.close()
                return None
            return n
        except OSError as e:
            if e.args[0] in (11, 115):  # EAGAIN / EINPROGRESS
                return None
            self.close()
            return None

    def write(self, buf):
        try:
            return self.sock.write(buf)
        except OSError:
            self.close()
            return None

    def ioctl(self, cmd, arg):
        if cmd == 4:  # MP_STREAM_POLL
            flags = 0
            try:
                r, w, _ = select.select([self.sock], [self.sock], [], 0)
                if r:
                    flags |= 1
                if w:
                    flags |= 4
            except Exception:
                flags |= 16
            return flags
        return 0

    def close(self):
        global _active_telnet_stream
        # Always release the session slot, even if detaching or closing the socket fails
        if _active_telnet_stream is self:
            _active_telnet_stream = None
            try:
                os.dupterm(None)
            except Exception:
                pass
        try:
            self.sock.close()
        except Exception:
            pass
        _set_low_latency(False)


_MAX_PASSWORD_LEN = 256  # Password plus the option negotiation some clients send first
_LOGIN_TICKS = 100  # 10s at one poll per 100ms daemon tick


class _TelnetLogin:
    """
    Collects a Telnet password without blocking the Core 1 daemon: poll() is called once per
    daemon tick, so beacons, OTA and Wi-Fi keep running while a client sits at the prompt.
    Note: Telnet is plaintext. The password keeps casual LAN users out; it does not
    protect against anyone who can sniff the network.
    """

    def __init__(self, sock):
        self.sock = sock
        self.buf = bytearray()
        self.ticks = 0
        self.done = False
        sock.setblocking(False)
        sock.write(b"Password: ")

    def poll(self, password):
        """Returns None while still waiting, otherwise True (accepted) or False (denied)."""
        self.ticks += 1
        try:
            r, _, _ = select.select([self.sock], [], [], 0)
            if r:
                chunk = self.sock.recv(64)
                if not chunk:
                    return False
                for b in chunk:
                    if b in (13, 10):
                        self.done = True
                        break
                    self.buf.append(b)
        except Exception:
            return False
        if self.done or len(self.buf) > _MAX_PASSWORD_LEN or self.ticks >= _LOGIN_TICKS:
            return self._verdict(password)
        return None

    def _verdict(self, password):
        # Drop Telnet option negotiation (IAC + 2 bytes) sent by clients like PuTTY
        clean = bytearray()
        i = 0
        while i < len(self.buf):
            if self.buf[i] == 255:
                i += 3
                continue
            clean.append(self.buf[i])
            i += 1
        try:
            pwd = bytes(clean).decode("utf-8").strip()
        except Exception:
            pwd = ""
        ok = (self.done and len(self.buf) <= _MAX_PASSWORD_LEN
              and _constant_time_compare(pwd, password))
        try:
            self.sock.write(b"\r\nAuthentication successful.\r\n" if ok else b"\r\nAccess denied.\r\n")
        except Exception:
            return False
        return ok


_MAX_PENDING_LOGINS = 3
_MAX_LOGIN_COOLDOWN = 600  # ticks (60s)


def _close_quietly(sock):
    try:
        sock.close()
    except Exception:
        pass


class _TelnetGate:
    """
    Accepts Telnet clients and runs their logins, a little per daemon tick.
    Several logins run side by side and a new client pushes out the oldest one still waiting,
    so an idle or hostile client can't hold the login slot and lock everyone else out.
    Wrong passwords back off exponentially (1s, 2s, 4s ... 60s) to slow down guessing;
    connections that time out or drop without sending a password don't count as guesses.
    A logged-in session is only replaced once the new client has authenticated.
    """

    def __init__(self, server_sock):
        self.server = server_sock
        self.pending = []
        self.cooldown = 0
        self.failures = 0

    def tick(self, password):
        if self.cooldown:
            self.cooldown -= 1
        for login in list(self.pending):
            try:
                verdict = login.poll(password)
            except Exception:
                verdict = False
            if verdict is None:
                continue
            self.pending.remove(login)
            if verdict:
                self.failures = 0
                self._open(login.sock)
            else:
                _close_quietly(login.sock)
                if login.done:
                    self.failures = min(self.failures + 1, 7)
                    self.cooldown = min(10 << (self.failures - 1), _MAX_LOGIN_COOLDOWN)

        if self.cooldown:
            return
        try:
            r, _, _ = select.select([self.server], [], [], 0)
            if not r:
                return
            client_sock, client_addr = self.server.accept()
        except Exception:
            return
        print(f"[Untethered] Telnet client connected from {client_addr}")
        if not password:
            self._open(client_sock)
            return
        if len(self.pending) >= _MAX_PENDING_LOGINS:
            _close_quietly(self.pending.pop(0).sock)
        try:
            self.pending.append(_TelnetLogin(client_sock))
        except Exception:
            _close_quietly(client_sock)

    def _open(self, client_sock):
        try:
            _open_telnet_session(client_sock)
        except Exception:
            _close_quietly(client_sock)


def _open_telnet_session(client_sock):
    """Replaces any current session and attaches the REPL to client_sock."""
    global _active_telnet_stream
    _cleanup_telnet()
    banner = (
        f"\r\n\033[1;36m====================================================\033[0m\r\n"
        f"\033[1;32m  Untethered Remote REPL [{_device_name}]\033[0m\r\n"
        f"  IP: {get_ip()} | Status: {_device_status} | Version: {get_version()}\r\n"
        f"  Escape character is Ctrl-] or close socket.\r\n"
        f"\033[1;36m====================================================\033[0m\r\n\r\n"
    )
    client_sock.write(banner.encode("utf-8"))
    _active_telnet_stream = _TelnetStream(client_sock)
    os.dupterm(_active_telnet_stream)
    _set_low_latency(True)


def _cleanup_telnet():
    if _active_telnet_stream:
        print("[Untethered] Telnet session closed. Restoring console.")
        _active_telnet_stream.close()
