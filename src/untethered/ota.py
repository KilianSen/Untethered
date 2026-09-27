"""
Untethered OTA: Differential File Update Engine & Remote Wipe
"""
import gc
import json
import os
import sys
import time

try:
    import machine
except ImportError:
    machine = None

try:
    import uhashlib as hashlib
    import ubinascii as binascii
except ImportError:
    import hashlib
    import binascii

try:
    import urequests
except ImportError:
    urequests = None

# Anti-replay counters (survive every wipe, including factory reset) and the pending-commit journal
_seq_file = ".untethered_seq.json"
_journal_file = ".ota_journal.json"
_components_allowed = ("app", "system")
_library_files = ("lib/untethered.py", "lib/untethered.mpy")
# boot.py starts the network services; a broken one would take OTA down with it. It is only
# written when OTA_ALLOW_BOOT_UPDATE is set, and never deleted or wiped by OTA.
_boot_file = "boot.py"
_probe_module = "_untethered_probe"


def _ensure_dir(path):
    parts = path.split("/")[:-1]
    curr = ""
    for p in parts:
        if not p or p == ".":
            continue
        curr = curr + "/" + p if curr else p
        try:
            os.mkdir(curr)
        except OSError:
            pass


def _compute_sha256(filepath):
    try:
        h = hashlib.sha256()
        with open(filepath, "rb") as f:
            while True:
                chunk = f.read(512)
                if not chunk:
                    break
                h.update(chunk)
        return binascii.hexlify(h.digest()).decode("ascii")
    except Exception:
        return None


def _composite_hash(files):
    """Deterministic component hash over sorted 'path:sha256' lines (must match tools/deploy.py)."""
    h = hashlib.sha256()
    for item in sorted(files, key=lambda x: x["path"]):
        h.update("{}:{}\n".format(item["path"], item["sha256"]).encode("utf-8"))
    return binascii.hexlify(h.digest()).decode("ascii")


def _component_of(path):
    return "system" if path.startswith("lib/") else "app"


def _is_safe_path(path):
    """Rejects absolute paths, traversal, and files the OTA engine must never overwrite."""
    if not isinstance(path, str) or not path:
        return False
    if path.startswith("/") or "\\" in path or path.endswith(".ota_new"):
        return False
    for part in path.split("/"):
        if part in ("", ".", ".."):
            return False
    return path not in ("config.py", _version_file, _seq_file, _journal_file)


def _load_seq():
    try:
        with open(_seq_file, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_seq(kind, value):
    data = _load_seq()
    data[kind] = value
    with open(_seq_file, "w") as f:
        json.dump(data, f)


def _seq_is_fresh(kind, seq, allow_equal=False):
    """Signed commands carry a monotonically increasing sequence number to block replays."""
    if not isinstance(seq, int) or isinstance(seq, bool):
        return False
    last = _load_seq().get(kind, 0)
    return seq >= last if allow_equal else seq > last


def get_version():
    try:
        with open(_version_file, "r") as f:
            data = json.load(f)
            return data.get("version", "0.0.0")
    except Exception:
        return "0.0.0"


def _get_local_state():
    try:
        with open(_version_file, "r") as f:
            return json.load(f)
    except Exception:
        return {"version": "0.0.0", "components": {}, "hashes": {}}


def _save_local_state(version, component_hashes, file_hashes):
    with open(_version_file, "w") as f:
        json.dump({"version": version, "components": component_hashes, "hashes": file_hashes}, f)


def _rmtree(path):
    """Recursively removes a directory tree or single file across MicroPython and CPython."""
    try:
        for entry in os.listdir(path):
            subpath = f"{path}/{entry}"
            _rmtree(subpath)
        os.rmdir(path)
    except OSError:
        try:
            os.remove(path)
        except OSError:
            pass


def _exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def _apply_journal(journal):
    """
    Rolls a verified update forward. Every staged file was hash-checked before the journal
    was written, so replaying this after a power loss converges on the new version.
    """
    for staging_path, target_path in journal.get("moves", []):
        if not _exists(staging_path):
            continue  # Already moved before the interruption
        _ensure_dir(target_path)
        try:
            os.remove(target_path)
        except OSError:
            pass
        os.rename(staging_path, target_path)
    for path in journal.get("deletes", []):
        _rmtree(path)
    _save_local_state(journal["version"], journal["components"], journal["hashes"])
    if journal.get("seq") is not None:
        _save_seq("manifest", journal["seq"])
    try:
        os.remove(_journal_file)
    except OSError:
        pass


def _resume_pending_update():
    """Completes an update that was interrupted mid-commit (called at boot by start())."""
    try:
        with open(_journal_file, "r") as f:
            journal = json.load(f)
    except Exception:
        return False
    print("[Untethered] Resuming interrupted update commit...")
    try:
        _apply_journal(journal)
        print(f"[Untethered] Interrupted update to v{journal.get('version')} completed.")
        return True
    except Exception as e:
        print(f"[Untethered] Could not resume update: {e}")
        return False


def wipe(scope="app", reboot=True):
    """
    Wipes board firmware according to scope ('app', 'system', or 'all').
    - 'app': removes application files and resets app hashes, preserving core & WiFi
    - 'system': removes lib/ files except the untethered runtime and resets system hashes,
      so the board stays reachable and the next deploy re-syncs the full system component
    - 'all': factory reset wiping all files from flash
    """
    print(f"[Untethered] Executing wipe with scope '{scope}'...")
    global _device_status
    if scope == "app":
        _device_status = "STANDBY"
        try:
            if "app" in os.listdir("."):
                _rmtree("app")
        except Exception:
            pass

        state = _get_local_state()
        local_hashes = state.get("hashes", {})
        for filepath in list(local_hashes.keys()):
            if not filepath.startswith("lib/") and filepath not in ("config.py", _version_file, _boot_file):
                try:
                    _rmtree(filepath)
                except Exception:
                    pass

        # Write clean Standby Stub to main.py
        try:
            with open("main.py", "w") as f:
                f.write("# Untethered Standby Stub\nimport untethered\nuntethered.start()\nprint('[Untethered] Board in STANDBY mode awaiting OTA deployment...')\n")
        except Exception as e:
            print(f"[Untethered] Notice: could not write standby stub: {e}")

        # An empty app hash marks STANDBY across the reboot and forces the next deploy to re-sync
        state.setdefault("components", {})["app"] = ""
        if "hashes" in state:
            state["hashes"] = {k: v for k, v in state["hashes"].items() if k.startswith("lib/") or k == _boot_file}
        try:
            with open(_version_file, "w") as f:
                json.dump(state, f)
        except Exception:
            pass
        print("[Untethered] Application wiped. Board entering STANDBY Mode.")

    elif scope == "system":
        # Keep the runtime itself: deleting it would take Wi-Fi and OTA down with it
        try:
            for entry in os.listdir("lib"):
                if "lib/" + entry not in _library_files:
                    _rmtree("lib/" + entry)
        except OSError:
            pass

        state = _get_local_state()
        state.setdefault("components", {})["system"] = ""
        state["hashes"] = {k: v for k, v in state.get("hashes", {}).items() if not k.startswith("lib/")}
        try:
            with open(_version_file, "w") as f:
                json.dump(state, f)
        except Exception:
            pass
        print("[Untethered] System libraries wiped. Runtime kept; next deploy re-syncs system.")

    elif scope in ("all", "factory"):
        print("[Untethered] Factory reset: wiping all flash storage...")
        # Same cwd-relative paths as every other OTA operation (cwd is "/" on the board)
        for entry in os.listdir("."):
            # Keep the anti-replay counters, or old signed commands could be replayed afterwards
            if entry not in (".", "..", _seq_file):
                _rmtree(entry)
        print("[Untethered] All flash storage cleared.")

    else:
        print(f"[Untethered] Unknown wipe scope '{scope}'. Nothing done.")
        return False

    if reboot and machine:
        print("[Untethered] Rebooting device...")
        try:
            time.sleep_ms(300)
        except Exception:
            pass
        machine.reset()
    return True


def wipe_app(reboot=True):
    """Convenience alias for wipe('app')."""
    return wipe("app", reboot=reboot)


def wipe_system(reboot=True):
    """Convenience alias for wipe('system')."""
    return wipe("system", reboot=reboot)


def wipe_all(reboot=True):
    """Convenience alias for wipe('all')."""
    return wipe("all", reboot=reboot)


def _probe_runtime(staging_path, rel_path):
    """
    Imports a downloaded runtime under a throwaway name before it replaces the running one.
    Catches the updates that would brick a board: an .mpy built for another MicroPython
    version, a syntax error, or a failing import. Returns an error string, or None if it loads.
    """
    ext = rel_path[rel_path.rfind("."):]
    if ext == ".mpy" and sys.implementation.name != "micropython":
        return None  # Host-side tests cannot load MicroPython bytecode
    probe_path = "lib/" + _probe_module + ext
    os.rename(staging_path, probe_path)
    sys.path.insert(0, "lib")
    try:
        try:
            import importlib
            importlib.invalidate_caches()
        except ImportError:
            pass
        __import__(_probe_module)
        return None
    except Exception as e:
        return str(e) or type(e).__name__
    finally:
        sys.path.remove("lib")
        sys.modules.pop(_probe_module, None)
        os.rename(probe_path, staging_path)
        gc.collect()


def _http_get(url):
    """GET with a socket timeout (connect and reads), so a host that vanishes mid-transfer
    cannot hang the Core 1 daemon and take Telnet, beacon and OTA down with it."""
    try:
        return urequests.get(url, timeout=_http_timeout)
    except TypeError:
        return urequests.get(url)  # Old urequests builds without timeout support


def _is_targeted(targets):
    """
    Whether a manifest's target list (device names and groups) covers this board. A board with
    a DEVICE_GROUP only takes manifests that name it, so a deploy without --target can never
    put another group's firmware on it; ungrouped boards also take untargeted manifests.
    """
    if targets is None:
        return not _device_group
    if not isinstance(targets, list):
        return True  # Malformed: not ours to skip, _validate_manifest rejects it
    return _device_name in targets or (bool(_device_group) and _device_group in targets)


def _signed_payload(manifest, app_h, sys_h):
    """What the manifest signature covers. Untargeted manifests keep the v2.1 format, so older
    boards still accept them; they reject targeted ones, which they could not honour."""
    payload = f"{manifest.get('version', 'unknown')}:{app_h}:{sys_h}:{manifest.get('seq')}"
    targets = manifest.get("targets")
    if targets is not None:
        payload += ":" + ",".join(targets)
    return payload


def _validate_manifest(manifest, key):
    """Returns an error string, or None if the manifest is well-formed and (if keyed) authentic."""
    components = manifest.get("components")
    if not isinstance(components, dict) or not components:
        return "Manifest has no components"
    targets = manifest.get("targets")
    if targets is not None and (not isinstance(targets, list) or not targets or not all(
            isinstance(t, str) and t and "," not in t for t in targets)):
        return "Malformed target list"

    for cname, cdata in components.items():
        if cname not in _components_allowed:
            return f"Unknown component '{cname}'"
        files = cdata.get("files", [])
        for item in files:
            path = item.get("path")
            sha = item.get("sha256")
            if not _is_safe_path(path):
                return f"Unsafe file path {repr(path)}"
            if path == _boot_file and not _allow_boot_update:
                return "boot.py updates are disabled on this board (set OTA_ALLOW_BOOT_UPDATE = True)"
            if _component_of(path) != cname:
                return f"File {path} listed under wrong component '{cname}'"
            if not isinstance(sha, str) or len(sha) != 64:
                return f"Missing or malformed sha256 for {path}"
        # Bind the file list to the (signed) component hash
        if _composite_hash(files) != cdata.get("hash"):
            return f"Component '{cname}' hash does not match its file list"

    if key:
        signature = manifest.get("signature")
        seq = manifest.get("seq")
        if not signature:
            return "Manifest is unsigned"
        app_h = components.get("app", {}).get("hash", "")
        sys_h = components.get("system", {}).get("hash", "")
        expected_sig = _compute_hmac_sha256(key, _signed_payload(manifest, app_h, sys_h))
        if not _constant_time_compare(signature, expected_sig):
            return "Signature mismatch"
        if not _seq_is_fresh("manifest", seq, allow_equal=True):
            return "Manifest is older than the installed one (replay/downgrade)"
    return None


def check_update(manifest_url=None, secret_key=None):
    """
    Checks for and applies an OTA update from manifest_url.
    Returns True if update was applied (device will reset), False otherwise.
    """
    url = manifest_url or _manifest_url
    if not url:
        print("[Untethered] OTA check aborted: no manifest URL configured.")
        return False

    if not urequests:
        print("[Untethered] Error: urequests library is required for OTA.")
        return False

    print(f"[Untethered] Checking updates at {url}...")
    try:
        res = _http_get(url)
        try:
            if res.status_code != 200:
                print(f"[Untethered] Manifest returned HTTP {res.status_code}")
                return False
            manifest = res.json()
        finally:
            res.close()
    except Exception as e:
        print(f"[Untethered] Could not fetch manifest: {e}")
        return False

    # Skipping is always safe, so this runs before the signature check: a manifest for another
    # group (maybe signed with that group's key) is skipped quietly instead of raising an alarm
    targets = manifest.get("targets") if isinstance(manifest, dict) else None
    if not _is_targeted(targets):
        print(f"[Untethered] Update is for {', '.join(str(t) for t in targets) if targets else 'ungrouped boards'}, "
              f"not this board. Skipping.")
        return False

    key = secret_key if secret_key is not None else _secret_key
    try:
        error = _validate_manifest(manifest, key)
    except Exception as e:
        error = f"Malformed manifest ({e})"
    if error:
        print(f"[Untethered] SECURITY ERROR: {error}. Rejecting update.")
        return False
    if key:
        print("[Untethered] Authenticity verified: HMAC-SHA256 signature is trusted.")

    remote_version = manifest.get("version", "unknown")
    components = manifest["components"]
    local_state = _get_local_state()
    local_version = local_state.get("version", "0.0.0")
    local_comp_hashes = local_state.get("components", {})
    local_file_hashes = local_state.get("hashes", {})
    new_comp = {k: v.get("hash", "") for k, v in components.items()}

    changed = [c for c, cdata in components.items() if cdata.get("hash") != local_comp_hashes.get(c)]
    if not changed and remote_version == local_version:
        print("[Untethered] Already up to date.")
        return False

    # Differential file check, plus files that disappeared from a changed component
    files_to_download = []
    deletes = []
    updated_file_hashes = dict(local_file_hashes)
    for cname in changed:
        remote_paths = set()
        for item in components[cname].get("files", []):
            remote_paths.add(item["path"])
            if local_file_hashes.get(item["path"]) != item["sha256"]:
                files_to_download.append(item)
        for path in local_file_hashes:
            if (_component_of(path) == cname and path not in remote_paths
                    and path not in _library_files and path != _boot_file):
                deletes.append(path)
                updated_file_hashes.pop(path, None)
        # MicroPython imports untethered.py before untethered.mpy, so a leftover runtime in the
        # other format would silently shadow the one this update installs
        if cname == "system" and any(p in remote_paths for p in _library_files):
            for path in _library_files:
                if path not in remote_paths and path not in deletes and _exists(path):
                    deletes.append(path)
                    updated_file_hashes.pop(path, None)

    if not files_to_download and not deletes:
        print("[Untethered] File contents match. Metadata updated.")
        _save_local_state(remote_version, new_comp, updated_file_hashes)
        if key:
            _save_seq("manifest", manifest["seq"])
        return False

    print(f"[Untethered] Downloading {len(files_to_download)} updated file(s)...")
    downloaded_staging = []

    try:
        for item in files_to_download:
            rel_path = item["path"]
            expected_hash = item["sha256"]
            staging_path = rel_path + ".ota_new"

            _ensure_dir(staging_path)
            print(f"[Untethered] Downloading: {rel_path}")

            f_res = _http_get(item["url"])
            try:
                if f_res.status_code != 200:
                    raise RuntimeError(f"HTTP {f_res.status_code} on {rel_path}")
                h = hashlib.sha256()
                downloaded_staging.append((staging_path, rel_path))
                with open(staging_path, "wb") as f_out:
                    while True:
                        if _wdt:
                            _wdt.feed()
                        chunk = f_res.raw.read(512)
                        if not chunk:
                            break
                        h.update(chunk)
                        f_out.write(chunk)
            finally:
                f_res.close()

            computed_hash = binascii.hexlify(h.digest()).decode("ascii")
            if computed_hash != expected_hash:
                raise ValueError(f"Checksum mismatch for {rel_path}")
            if rel_path in _library_files:
                error = _probe_runtime(staging_path, rel_path)
                if error:
                    raise ValueError(f"New runtime {rel_path} does not load on this board ({error})")
            updated_file_hashes[rel_path] = computed_hash

    except Exception as e:
        print(f"[Untethered] Update aborted due to error: {e}. Cleaning up...")
        for staging_path, _ in downloaded_staging:
            try:
                os.remove(staging_path)
            except Exception:
                pass
        return False

    # Journaled commit: once the journal exists, a power loss is recovered at next boot
    journal = {
        "version": remote_version,
        "components": new_comp,
        "hashes": updated_file_hashes,
        "moves": downloaded_staging,
        "deletes": deletes,
        # Recorded in the journal so a power loss after the commit cannot lose the counter
        "seq": manifest["seq"] if key else None,
    }
    print("[Untethered] Verification successful. Applying updates...")
    with open(_journal_file, "w") as f:
        json.dump(journal, f)
    _apply_journal(journal)
    print(f"[Untethered] Update applied cleanly to v{remote_version}. Rebooting in 1s...")

    time.sleep(1)
    if machine:
        machine.reset()
    return True
