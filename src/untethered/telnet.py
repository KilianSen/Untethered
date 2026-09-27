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
