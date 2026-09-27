"""
Host-side tests for the bundled runtime (dist/untethered.py) and tools/deploy.py.
The device code is exercised under CPython with a fake urequests and a temp dir as flash.

Run: python -m unittest discover tests
"""
import hashlib
import hmac
import http.client
import importlib.util
import io
import json
import os
import shutil
import socketserver
import sys
import tempfile
import threading
import types
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import deploy  # noqa: E402

SECRET = "test-secret"


def load_runtime():
    spec = importlib.util.spec_from_file_location("untethered", os.path.join(ROOT, "dist", "untethered.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.time = types.SimpleNamespace(sleep=lambda s: None, sleep_ms=lambda ms: None, time=lambda: 0)
    mod.print = lambda *a, **k: None
    return mod


class FakeResponse:
    def __init__(self, body, status=200):
        self.status_code = status
        self.raw = io.BytesIO(body)
        self._body = body

    def json(self):
        return json.loads(self._body)

    def close(self):
        pass


class FakeRequests:
    """Serves URLs from a {url_path: bytes} map, like the deploy HTTP server would."""

    def __init__(self):
        self.routes = {}
        self.timeouts = []

    def get(self, url, timeout=None):
        self.timeouts.append(timeout)
        path = "/" + url.split("/", 3)[3]
        if path not in self.routes:
            return FakeResponse(b"", 404)
        return FakeResponse(self.routes[path])


class RuntimeTestCase(unittest.TestCase):
    def setUp(self):
        self.orig_cwd = os.getcwd()
        self.tmp = tempfile.mkdtemp()
        self.device = os.path.join(self.tmp, "device")
        self.host = os.path.join(self.tmp, "host")
        self.project = os.path.join(self.host, "project")
        os.makedirs(self.device)
        os.makedirs(os.path.join(self.host, "dist"))
        os.makedirs(self.project)
        with open(os.path.join(self.host, "dist", "untethered.mpy"), "wb") as f:
            f.write(b"runtime-v1")
        self.write_project({"main.py": b"print('v1')\n", "helpers/util.py": b"X = 1\n"})

        os.chdir(self.device)
        self.rt = load_runtime()
        self.req = FakeRequests()
        self.rt.urequests = self.req
        self.rt._manifest_url = "http://host:8000/manifest.json"
        self.rt._secret_key = SECRET

    def tearDown(self):
        os.chdir(self.orig_cwd)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_project(self, files):
        for rel, data in files.items():
            path = os.path.join(self.project, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(data)

    def publish(self, secret=SECRET, seq=None, mutate=None):
        """Builds a manifest with the real deploy tool and serves it through the fake HTTP layer."""
        manifest, served = deploy.build_manifest(self.host, self.project, "host", 8000, "1.0.0", secret=secret)
        if seq is not None:
            manifest["seq"] = seq
            if secret:
                c = manifest["components"]
                manifest["signature"] = deploy.compute_signature(
                    secret, "1.0.0", c["app"]["hash"], c["system"]["hash"], seq)
        if mutate:
            mutate(manifest, served)
        self.req.routes = {}
        for rel, path in served.items():
            with open(path, "rb") as f:
                self.req.routes["/" + rel] = f.read()
        self.req.routes["/manifest.json"] = json.dumps(manifest).encode()
        return manifest

    def read(self, rel):
        with open(os.path.join(self.device, rel), "rb") as f:
            return f.read()


class CryptoTests(RuntimeTestCase):
    def test_hmac_matches_stdlib(self):
        for key in ("k", "x" * 100):
            expected = hmac.new(key.encode(), b"msg", hashlib.sha256).hexdigest()
            self.assertEqual(self.rt._compute_hmac_sha256(key, "msg"), expected)

    def test_composite_hash_matches_deploy(self):
        files = [{"path": "b.py", "sha256": "1" * 64}, {"path": "a.py", "sha256": "2" * 64}]
        self.assertEqual(self.rt._composite_hash(files), deploy.compute_composite_hash(files))


class UpdateTests(RuntimeTestCase):
    def test_signed_update_installs_files(self):
        self.publish()
        self.assertTrue(self.rt.check_update())
        self.assertEqual(self.read("main.py"), b"print('v1')\n")
        self.assertEqual(self.read("helpers/util.py"), b"X = 1\n")
        self.assertEqual(self.read("lib/untethered.mpy"), b"runtime-v1")
        self.assertFalse(os.path.exists(".ota_journal.json"))
        self.assertFalse(self.rt.check_update(), "second check should be up to date")
        self.assertNotIn(None, self.req.timeouts, "every request needs a timeout")

    def test_switching_runtime_format_removes_the_old_one(self):
        # A leftover untethered.py would shadow the new .mpy on import
        os.makedirs("lib")
        with open("lib/untethered.py", "w") as f:
            f.write("OLD = 1\n")
        self.rt._save_local_state("0.9.0", {"system": "old"}, {"lib/untethered.py": "0" * 64})
        self.publish(seq=1)
        self.assertTrue(self.rt.check_update())
        self.assertFalse(os.path.exists("lib/untethered.py"))
        self.assertEqual(self.read("lib/untethered.mpy"), b"runtime-v1")
        self.assertNotIn("lib/untethered.py", self.rt._get_local_state()["hashes"])

    def test_tampered_file_list_rejected_despite_valid_signature(self):
        def swap_payload(manifest, served):
            evil = os.path.join(self.tmp, "evil.py")
            with open(evil, "wb") as f:
                f.write(b"evil()")
            entry = manifest["components"]["app"]["files"][0]
            entry["sha256"] = deploy.compute_sha256(evil)
            served[entry["path"]] = evil

        self.publish(mutate=swap_payload)
        self.assertFalse(self.rt.check_update())
        self.assertFalse(os.path.exists("main.py"))

    def test_unsigned_manifest_rejected_when_key_set(self):
        self.publish(secret=None)
        self.assertFalse(self.rt.check_update())
        self.assertFalse(os.path.exists("main.py"))

    def test_unsafe_paths_rejected(self):
        for bad in ("../escape.py", "/abs.py", "config.py", "a\\..\\b.py", "version.json"):
            def inject(manifest, served, bad=bad):
                files = manifest["components"]["app"]["files"]
                files.append({"path": bad, "url": "http://host:8000/x", "sha256": "0" * 64})
                manifest["components"]["app"]["hash"] = deploy.compute_composite_hash(files)

            self.rt._secret_key = None
            self.publish(secret=None, mutate=inject)
            self.assertFalse(self.rt.check_update(), bad)
            self.assertFalse(os.path.exists("main.py"), bad)

    def test_replayed_older_manifest_rejected(self):
        self.publish(seq=2000)
        self.assertTrue(self.rt.check_update())
        self.write_project({"main.py": b"print('old but signed')\n"})
        self.publish(seq=1000)
        self.assertFalse(self.rt.check_update())
        self.assertEqual(self.read("main.py"), b"print('v1')\n")

    def test_removed_files_are_deleted(self):
        self.publish(seq=1)
        self.rt.check_update()
        os.remove(os.path.join(self.project, "helpers", "util.py"))
        self.publish(seq=2)
        self.assertTrue(self.rt.check_update())
        self.assertFalse(os.path.exists("helpers/util.py"))
        self.assertTrue(os.path.exists("lib/untethered.mpy"))

    def test_interrupted_commit_is_resumed(self):
        with open("main.py.ota_new", "wb") as f:
            f.write(b"new")
        journal = {"version": "2.0.0", "components": {"app": "h"}, "hashes": {"main.py": "x"},
                   "moves": [["main.py.ota_new", "main.py"]], "deletes": []}
        with open(".ota_journal.json", "w") as f:
            json.dump(journal, f)
        self.assertTrue(self.rt._resume_pending_update())
        self.assertEqual(self.read("main.py"), b"new")
        self.assertEqual(self.rt.get_version(), "2.0.0")
        self.assertFalse(os.path.exists(".ota_journal.json"))


class ProvisionAndWipeTests(RuntimeTestCase):
    def test_provision_escapes_special_characters(self):
        password = 'pa"ss\\word\'\n'
        self.rt.provision("My WiFi", password, name="dev-1", boot=False, auto_start=False)
        ns = {}
        with open("config.py") as f:
            exec(f.read(), ns)
        self.assertEqual(ns["WIFI_PASSWORD"], password)
        self.assertEqual(ns["WIFI_SSID"], "My WiFi")
        self.assertNotIn("OTA_MANIFEST_URL", ns)

    def test_provision_writes_optional_settings(self):
        self.rt.provision("net", "pw", name="dev-1", boot=False, auto_start=False,
                          manifest_url="http://10.0.0.5:8000/manifest.json",
                          telnet_password="tpw", secret_key="k")
        ns = {}
        with open("config.py") as f:
            exec(f.read(), ns)
        self.assertEqual(ns["OTA_MANIFEST_URL"], "http://10.0.0.5:8000/manifest.json")
        self.assertEqual(ns["TELNET_PASSWORD"], "tpw")
        self.assertEqual(ns["OTA_SECRET_KEY"], "k")

    def test_system_wipe_keeps_runtime(self):
        self.publish()
        self.rt.check_update()
        with open("lib/other.py", "w") as f:
            f.write("x")
        self.rt.wipe("system", reboot=False)
        self.assertTrue(os.path.exists("lib/untethered.mpy"))
        self.assertFalse(os.path.exists("lib/other.py"))
        self.assertEqual(self.rt._get_local_state()["components"]["system"], "")

    def test_remote_wipe_requires_signature_and_rejects_replay(self):
        calls = []
        self.rt.wipe = lambda scope, reboot=True: calls.append(scope)
        self.rt._device_name = "dev-1"

        self.rt._secret_key = None
        self.rt._handle_remote_wipe({"cmd": "wipe", "scope": "all"}, "all")
        self.assertEqual(calls, [], "unsigned mode must ignore remote wipe")

        self.rt._secret_key = SECRET
        msg = {"scope": "app", "seq": 10,
               "signature": deploy.compute_wipe_signature(SECRET, "app", "dev-1", 10)}
        self.rt._handle_remote_wipe(dict(msg), "dev-1")
        self.rt._handle_remote_wipe(dict(msg), "dev-1")  # replay
        self.rt._handle_remote_wipe(dict(msg, scope="all"), "dev-1")  # scope not covered by signature
        self.assertEqual(calls, ["app"])

    def test_unsigned_push_cannot_redirect_manifest(self):
        urls = []
        self.rt.check_update = lambda url=None: urls.append(url)
        self.rt._secret_key = None
        self.rt._handle_push({"cmd": "ota", "url": "http://attacker/manifest.json"})
        self.assertEqual(urls, ["http://host:8000/manifest.json"])

    def test_unsigned_push_without_manifest_url_needs_opt_in(self):
        urls = []
        self.rt.check_update = lambda url=None: urls.append(url)
        self.rt._secret_key = None
        self.rt._manifest_url = None
        self.rt._handle_push({"cmd": "ota", "url": "http://dev/manifest.json"})
        self.assertEqual(urls, [])
        self.rt._allow_unsigned_push = True
        self.rt._handle_push({"cmd": "ota", "url": "http://dev/manifest.json"})
        self.assertEqual(urls, ["http://dev/manifest.json"])

    def test_push_flood_is_rate_limited(self):
        urls = []
        now = [1000]
        self.rt.check_update = lambda url=None: urls.append(url)
        self.rt.time = types.SimpleNamespace(time=lambda: now[0])
        for _ in range(50):
            self.rt._handle_push({"cmd": "ota"})
        self.assertEqual(len(urls), 1)
        now[0] += self.rt._PUSH_COOLDOWN_S
        self.rt._handle_push({"cmd": "ota"})
        self.assertEqual(len(urls), 2)
        now[0] -= 3600  # Clock stepped back (e.g. NTP) must not block pushes for an hour
        self.rt._handle_push({"cmd": "ota"})
        self.assertEqual(len(urls), 3)


class ProtectionTests(RuntimeTestCase):
    def test_boot_py_skipped_by_default(self):
        self.write_project({"boot.py": b"import untethered\n"})
        manifest = self.publish()
        paths = [f["path"] for f in manifest["components"]["app"]["files"]]
        self.assertNotIn("boot.py", paths)

    def test_boot_py_rejected_unless_board_allows_it(self):
        self.write_project({"boot.py": b"import untethered\n"})

        def include_boot(manifest, served):
            path = os.path.join(self.project, "boot.py")
            files = manifest["components"]["app"]["files"]
            files.append({"path": "boot.py", "url": "http://host:8000/boot.py",
                          "sha256": deploy.compute_sha256(path)})
            manifest["components"]["app"]["hash"] = deploy.compute_composite_hash(files)
            served["boot.py"] = path

        self.rt._secret_key = None
        self.publish(secret=None, mutate=include_boot)
        self.assertFalse(self.rt.check_update())
        self.assertFalse(os.path.exists("main.py"))

        self.rt._allow_boot_update = True
        self.assertTrue(self.rt.check_update())
        self.assertEqual(self.read("boot.py"), b"import untethered\n")

    def test_boot_py_never_deleted_by_update_or_app_wipe(self):
        self.rt._allow_boot_update = True
        with open("boot.py", "w") as f:
            f.write("import untethered\n")
        self.rt._save_local_state("0.9.0", {"app": "old"}, {"boot.py": "0" * 64, "old.py": "1" * 64})
        with open("old.py", "w") as f:
            f.write("x")
        self.publish(seq=1)
        self.assertTrue(self.rt.check_update())
        self.assertTrue(os.path.exists("boot.py"))
        self.assertFalse(os.path.exists("old.py"))
        self.rt.wipe("app", reboot=False)
        self.assertTrue(os.path.exists("boot.py"))

    def test_runtime_that_fails_to_load_is_rejected(self):
        self.write_project({"lib/untethered.py": b"def broken(:\n"})
        self.publish(seq=1)
        self.assertFalse(self.rt.check_update())
        self.assertFalse(os.path.exists("lib/untethered.py"))
        self.assertFalse(os.path.exists("lib/untethered.py.ota_new"))
        self.assertFalse(os.path.exists("lib/_untethered_probe.py"))

        self.write_project({"lib/untethered.py": b"X = 1\n"})
        self.publish(seq=2)
        self.assertTrue(self.rt.check_update())
        self.assertEqual(self.read("lib/untethered.py"), b"X = 1\n")
        self.assertNotIn("_untethered_probe", sys.modules)

    def test_journal_records_seq(self):
        journal = {"version": "2.0.0", "components": {}, "hashes": {}, "moves": [], "deletes": [], "seq": 42}
        with open(".ota_journal.json", "w") as f:
            json.dump(journal, f)
        self.assertTrue(self.rt._resume_pending_update())
        self.assertFalse(self.rt._seq_is_fresh("manifest", 41, allow_equal=True))

    def test_app_wipe_reports_standby_after_reboot(self):
        # The wipe reboots, so the status must survive in flash; also on a never-updated board
        self.rt.wipe("app", reboot=False)
        self.assertEqual(load_runtime()._initial_status(), "STANDBY")
        self.publish(seq=1)
        self.assertTrue(self.rt.check_update())
        self.assertEqual(load_runtime()._initial_status(), "RUNNING")

    def test_factory_reset_keeps_replay_counters(self):
        self.rt._save_seq("wipe", 10)
        with open("main.py", "w") as f:
            f.write("x")
        self.rt.wipe("all", reboot=False)
        self.assertFalse(os.path.exists("main.py"))
        self.assertFalse(self.rt._seq_is_fresh("wipe", 10))


class FakeSock:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.sent = b""

    def write(self, data):
        self.sent += data

    def setblocking(self, flag):
        pass

    def recv(self, n):
        return self.chunks.pop(0)

    def close(self):
        self.closed = True


class FakeServer:
    """Listening socket; `chunks` holds clients waiting to be accepted (the select fake reads it)."""

    def __init__(self):
        self.chunks = []

    def accept(self):
        return self.chunks.pop(0), ("10.0.0.9", 1234)


class NetworkTests(RuntimeTestCase):
    def setUp(self):
        super().setUp()
        self.rt.select = types.SimpleNamespace(
            select=lambda r, w, x, t: ([s for s in r if getattr(s, "chunks", None)], [], []))

    def login(self, sock, password="pwd"):
        """Drives a login the way the daemon does: one poll per tick. Returns (verdict, ticks)."""
        login = self.rt._TelnetLogin(sock)
        ticks = 0
        while True:
            ticks += 1
            verdict = login.poll(password)
            if verdict is not None:
                return verdict, ticks

    def test_telnet_auth_accepts_correct_password(self):
        sock = FakeSock([b"\xff\xfb\x01pw", b"d\r\n"])
        self.assertEqual(self.login(sock), (True, 2))
        self.assertFalse(self.login(FakeSock([b"nope\r\n"]))[0])

    def test_telnet_auth_is_bounded_against_endless_input(self):
        sock = FakeSock([b"a" * 64] * 1000)  # Never sends a newline
        self.assertFalse(self.login(sock)[0])
        self.assertGreater(len(sock.chunks), 900, "should give up once the buffer cap is hit")

    def test_telnet_auth_times_out(self):
        self.assertEqual(self.login(FakeSock([])), (False, 100))

    def test_telnet_login_never_blocks(self):
        sleeps = []
        self.rt.time = types.SimpleNamespace(sleep_ms=sleeps.append, sleep=sleeps.append)
        self.assertIsNone(self.rt._TelnetLogin(FakeSock([])).poll("pwd"))
        self.assertEqual(sleeps, [])

    def gate(self):
        opened = []
        self.rt._open_telnet_session = opened.append
        server = FakeServer()
        return self.rt._TelnetGate(server), server, opened

    def test_idle_client_cannot_hold_the_login_slot(self):
        gate, server, opened = self.gate()
        idle = [FakeSock([]) for _ in range(self.rt._MAX_PENDING_LOGINS)]
        for sock in idle:
            server.chunks.append(sock)
            gate.tick("pwd")
        user = FakeSock([b"pwd\r\n"])
        server.chunks.append(user)
        gate.tick("pwd")  # Accepted: the oldest idle login is pushed out
        self.assertTrue(getattr(idle[0], "closed", False))
        gate.tick("pwd")
        self.assertEqual(opened, [user])

    def test_wrong_passwords_back_off_but_timeouts_do_not(self):
        gate, server, opened = self.gate()
        server.chunks.append(FakeSock([]))
        gate.tick("pwd")
        for _ in range(self.rt._LOGIN_TICKS):
            gate.tick("pwd")
        self.assertEqual((gate.pending, gate.cooldown), ([], 0), "a timeout is not a guess")

        cooldowns = []
        for _ in range(3):
            server.chunks.append(FakeSock([b"guess\r\n"]))
            while not gate.cooldown:
                gate.tick("pwd")
            cooldowns.append(gate.cooldown)
            while gate.cooldown:
                gate.tick("pwd")
        self.assertEqual(cooldowns, [10, 20, 40])

        server.chunks.append(FakeSock([b"pwd\r\n"]))
        gate.tick("pwd")
        gate.tick("pwd")
        self.assertEqual(len(opened), 1)
        self.assertEqual(gate.failures, 0)

    def test_telnet_off_when_key_set_without_password(self):
        started = []

        class FakeWLAN:
            def __init__(self, iface):
                pass

            def active(self, flag):
                pass

            def isconnected(self):
                return True

            def ifconfig(self):
                return ("10.0.0.2",)

            def config(self, **kw):
                pass

        self.rt.network = types.SimpleNamespace(WLAN=FakeWLAN, STA_IF=0)
        self.rt._thread = types.SimpleNamespace(start_new_thread=lambda f, args: started.append(args))
        self.rt.start(secret_key=SECRET, telnet=True, ota_interval=0)
        self.assertEqual(started, [(0, False)])

        rt = load_runtime()
        rt.network, rt._thread = self.rt.network, self.rt._thread
        rt.start(secret_key=SECRET, telnet=True, telnet_password="pwd", ota_interval=0)
        self.assertEqual(started[-1], (0, True))

    def test_async_app_is_awaited(self):
        ran = []

        async def main():
            ran.append(True)
            raise RuntimeError("stop")

        self.rt._initialized = True
        self.rt.app(main)
        self.assertEqual(ran, [True])
        self.assertEqual(self.rt._device_status, "CRASHED")

    def test_app_returns_after_crash_so_the_repl_works(self):
        # Idling on Core 0 would leave the Telnet REPL unable to take commands
        def main():
            raise ValueError("boom")

        self.rt._initialized = True
        self.assertIs(self.rt.app(main), main)
        self.assertEqual(self.rt._device_status, "CRASHED")
        self.assertEqual(self.rt._last_error, "boom")

        self.rt.app(lambda: None)
        self.assertEqual(self.rt._device_status, "STOPPED")

    def test_wifi_reconnects_when_link_drops(self):
        calls = []

        class FakeWLAN:
            connected = False

            def __init__(self, iface):
                pass

            def isconnected(self):
                return FakeWLAN.connected

            def status(self):
                return 0

            def active(self, flag):
                pass

            def connect(self, ssid, pwd=None):
                calls.append((ssid, pwd))

        self.rt.network = types.SimpleNamespace(WLAN=FakeWLAN, STA_IF=0, STAT_CONNECTING=1)
        self.rt._wifi_ssid, self.rt._wifi_password = "net", "pw"
        self.assertFalse(self.rt._reconnect_wifi())
        self.assertEqual(calls, [("net", "pw")])
        FakeWLAN.connected = True
        self.assertTrue(self.rt._reconnect_wifi())
        self.assertEqual(len(calls), 1)

        FakeWLAN.connected = False
        self.rt._wifi_password = None  # Open network
        self.assertFalse(self.rt._reconnect_wifi())
        self.assertEqual(calls[-1], ("net", None))


class ToolTests(unittest.TestCase):
    def test_missing_project_dir_is_an_error(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(FileNotFoundError):
                deploy.build_manifest(root, os.path.join(root, "typo"), "h", 1, "1.0.0")

    def test_seq_never_goes_backwards_with_the_clock(self):
        orig = deploy.time.time
        try:
            with tempfile.TemporaryDirectory() as root:
                deploy.time.time = lambda: 2000
                first = deploy.next_seq(root)
                deploy.time.time = lambda: 1000  # Clock stepped back
                self.assertGreater(deploy.next_seq(root), first)
                self.assertGreater(deploy.next_seq(root), first + 1)
        finally:
            deploy.time.time = orig

    def test_config_py_excluded_only_at_project_root(self):
        with tempfile.TemporaryDirectory() as root:
            proj = os.path.join(root, "p")
            os.makedirs(os.path.join(proj, "app"))
            for rel in ("main.py", "config.py", "app/config.py"):
                with open(os.path.join(proj, rel), "w") as f:
                    f.write("x")
            manifest, _ = deploy.build_manifest(root, proj, "h", 1, "1.0.0")
            paths = [f["path"] for f in manifest["components"]["app"]["files"]]
            self.assertEqual(sorted(paths), ["app/config.py", "main.py"])

    def test_host_ip_prefers_default_route_lan_address(self):
        orig = deploy._default_route_ip, deploy.socket.gethostbyname_ex
        try:
            deploy._default_route_ip = lambda: "192.168.1.50"
            # Windows often lists a Hyper-V/WSL adapter first; Tailscale is 100.64/10
            deploy.socket.gethostbyname_ex = lambda h: (h, [], ["172.20.0.1", "100.101.1.2", "192.168.1.50"])
            self.assertEqual(deploy.get_local_ip(), "192.168.1.50")
            self.assertEqual(deploy.lan_ip_candidates(), ["192.168.1.50", "172.20.0.1"])

            deploy._default_route_ip = lambda: "100.101.1.2"  # VPN holds the default route
            self.assertEqual(deploy.get_local_ip(), "172.20.0.1")
            self.assertEqual(deploy.get_local_ip("10.1.1.1"), "10.1.1.1")
        finally:
            deploy._default_route_ip, deploy.socket.gethostbyname_ex = orig

    def test_bundler_strips_only_module_docstring(self):
        import bundle
        src = ['"""Module doc."""\n', "def f():\n", '    """\n', "    Doc.\n", '    """\n', "\n", "X = f\n"]
        self.assertEqual(bundle.strip_module_docstring(src), src[1:])


class DeployServerTests(RuntimeTestCase):
    def test_server_only_serves_manifest_files(self):
        with open(os.path.join(self.host, "config.py"), "w") as f:
            f.write("OTA_SECRET_KEY = 'leak'")
        _, served = deploy.build_manifest(self.host, self.project, "127.0.0.1", 0, "1.0.0", secret=SECRET)
        handler = type("H", (deploy.ManifestHTTPRequestHandler,), {"served_files": served,
                                                                     "log_message": lambda *a: None})
        httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            def status(path):
                conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1])
                conn.request("GET", path)
                code = conn.getresponse().status
                conn.close()
                return code

            self.assertEqual(status("/main.py"), 200)
            self.assertEqual(status("/helpers/util.py"), 200)
            self.assertEqual(status("/manifest.json"), 200)
            self.assertEqual(status("/lib/untethered.mpy"), 200)
            self.assertEqual(status("/config.py"), 404)
            self.assertEqual(status("/..\\..\\config.py"), 404)
            self.assertEqual(status("/../config.py"), 404)
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
