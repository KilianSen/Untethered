# Untethered: Wireless Updates & Remote REPL for Raspberry Pi Pico W

Put your Pico W somewhere hard to reach and keep working on it over Wi-Fi:

* **Update your code wirelessly.** Change your program on your PC, run one command, and every board picks it up and restarts.
* **See what your board is doing.** Connect over the network to watch its output, and type Python commands once your program has stopped.
* **Crashes don't strand you.** If your program crashes, or you upload a file with a typo in it, the board stays online so you can send a fix.

It is a small MicroPython library for the board plus a few Python scripts for your PC.

---

## Contents

1. [What you need](#1-what-you-need)
2. [Set up a board (about 10 minutes)](#2-set-up-a-board-about-10-minutes)
3. [Your first wireless update](#3-your-first-wireless-update)
4. [Find and watch your boards](#4-find-and-watch-your-boards)
5. [The remote REPL (Telnet)](#5-the-remote-repl-telnet)
6. [Troubleshooting](#6-troubleshooting)
7. [Settings reference (`config.py`)](#7-settings-reference-configpy)
8. [Security](#8-security)
9. [Wiping and resetting boards](#9-wiping-and-resetting-boards)
10. [How it works](#10-how-it-works)
11. [Known limitations](#11-known-limitations)
12. [Development](#12-development)

---

## 1. What you need

* A **Raspberry Pi Pico W** (the Wi-Fi version) and a USB cable for the first setup.
* A **2.4 GHz Wi-Fi network**. The Pico W cannot join 5 GHz-only networks.
* A PC (Windows, macOS or Linux) on the same network, with **Python 3.8 or newer**.
* **mpremote**, the official tool for talking to MicroPython boards over USB:
  ```bash
  pip install mpremote
  ```
* **This repository** on your PC, either with `git clone https://github.com/KilianSen/Untethered` or by downloading the *Source code (zip)* of the newest release from the [releases page](https://github.com/KilianSen/Untethered/releases) (see [which version gets installed](#step-4-install-and-configure-the-board)). The PC tools live in its `tools/` folder, and all commands below are run from the repository folder.

---

## 2. Set up a board (about 10 minutes)

### Step 1: Install MicroPython

Download the Pico W firmware (`.uf2`) from [micropython.org/download/RPI_PICO_W](https://micropython.org/download/RPI_PICO_W/). Hold the **BOOTSEL** button while plugging in the board. It shows up as a USB drive; copy the `.uf2` file onto it. The board restarts into MicroPython.

MicroPython 1.23 or newer is recommended (tested on 1.27).

### Step 2: Find your update address

Boards download updates from your PC, so they need to know your PC's address. Run:

```bash
python tools/deploy.py --dry-run
```

and look for the line

```text
 Manifest URL:     http://192.168.1.50:8000/manifest.json  (boards' OTA_MANIFEST_URL)
```

Copy that URL. It only stays valid while your PC keeps the same IP address.

> **Not sure whether you need a fixed IP?** If your router lets you, give your PC a "DHCP reservation" (usually under *LAN* or *DHCP* settings, look for your PC's name). If it doesn't, skip it: most home routers hand out the same address again anyway. If the address ever changes, boards stop updating and `deploy.py` prints a different *Manifest URL*; then set the new one on each board (see [Troubleshooting](#6-troubleshooting)).

### Step 3: Choose two passwords

* A **Telnet password** that protects the remote Python prompt.
* An **update key** (signing secret) that makes sure boards only accept updates from you. Any long random text works, e.g. `correct-horse-battery-staple-42`.

> **Not sure? Set both.** It costs nothing and protects you from anyone else on your network (a guest's phone, a compromised smart plug). Skipping them only makes sense for a quick experiment that you wipe afterwards. Save both in your password manager: you need the update key every time you deploy, and without it you can only update the board over USB again.

### Step 4: Install and configure the board

With the board plugged in over USB, run these two commands. Replace the Wi-Fi name, Wi-Fi password, board name, URL, Telnet password and update key with your own:

```bash
mpremote mip install github:KilianSen/Untethered

mpremote exec "import untethered; untethered.provision('MyWiFi', 'MyWiFiPassword', name='workshop-pico', manifest_url='http://192.168.1.50:8000/manifest.json', telnet_password='my-telnet-password', secret_key='my-update-key')"
```

The first command copies the library onto the board. The second one:

1. writes a `config.py` on the board with your settings,
2. writes a two-line `boot.py` that starts the network services every time the board powers on,
3. connects to Wi-Fi and prints the board's IP address.

Unplug the board and plug it into any USB power supply. From now on you can work on it over Wi-Fi.

> **Which version gets installed?** `github:KilianSen/Untethered` installs whatever is on the `main` branch at that moment, not necessarily a published release. `main` is usually fine, but it can contain changes that haven't been released yet. To install a specific release, add its tag:
>
> ```bash
> mpremote mip install github:KilianSen/Untethered@v2.1.0
> ```
>
> The available versions are listed on the [releases page](https://github.com/KilianSen/Untethered/releases). The same `@` works for any branch or commit, so only use tags you trust.
>
> **Not sure?** Use the newest release tag. Also keep in mind that `deploy.py` sends the library from *your copy* of this repository to your boards. If you cloned `main`, your boards get `main`; run `git checkout v2.1.0` in the repository folder to match the release.

> Settings can be changed later by editing `config.py` on the board (over USB, or from the remote REPL). All options are listed in the [settings reference](#7-settings-reference-configpy).

---

## 3. Your first wireless update

### Step 1: Make a project folder

A project is just a folder with your program in it. Copy `examples/simple_project/main.py` into a new folder, for example `C:/pico/my-project/`. Its core looks like this:

```python
import time
import untethered


@untethered.app
def main():
    counter = 0
    while True:
        counter += 1
        print("Iteration", counter)
        time.sleep(2)
```

Put your own code inside `main()`. The `@untethered.app` line tells Untethered to run it and to keep the board reachable if it crashes. `async def main()` works too, if you use `asyncio`.

> **Not sure whether to use `def` or `async def`?** Use plain `def`. Only switch to `async def` if you follow a guide or library that uses `asyncio` (you'll recognise it by `await` and `asyncio.create_task`). And always keep `@untethered.app`: without it, a crash leaves no error for the remote REPL to show you.

You can add more files and folders (helper modules, data files, a `lib/` folder with libraries); they are all sent to the board. You don't need a `boot.py` or `config.py` in the project, because the board already has its own.

### Step 2: Send it to your boards

```bash
python tools/deploy.py --project-dir C:/pico/my-project --secret "my-update-key"
```

This starts a small web server on your PC and tells all boards that an update is ready. Each board downloads the files that changed, checks them, installs them and restarts. **Keep the command running until your boards have restarted**, then stop it with **Ctrl+C**.

Useful options:

| Option | What it does |
| :--- | :--- |
| `--target workshop-pico` | Update only this board (try an update on one board before the others). |
| `--version 1.4.0` | Label the update. Without it, a timestamp like `dev-20260927-220106` is used. Boards show it in the fleet monitor. |
| `--host-ip 192.168.1.50` | The address boards download from. Only needed if `deploy.py` picked the wrong network (it lists other addresses it found). |
| `--no-push` | Serve the update without notifying boards. They pick it up on their next scheduled check (see `OTA_CHECK_INTERVAL`). |
| `--dry-run` | Only show what would be sent. |

> **Not sure which options you need?** None, to start with. Add `--target` once you have several boards and want to try a risky change on one of them first. Add `--version` when you start caring which board runs which code; a number you raise with every real change (`1.0`, `1.1`, …) is easier to read than a timestamp. `--no-push` is rarely needed.

> Tired of typing `--secret`? Set the environment variable `UNTETHERED_SECRET` to your update key, or create a `config.py` with `OTA_SECRET_KEY = "..."` in the repository folder. `deploy.py` picks up either. **Not sure which?** The `config.py` file is the easiest: it's a one-time step and Git already ignores it, so it won't end up on GitHub by accident.

Always pass `--project-dir`. Without it, `deploy.py` uses the current folder if it contains a `main.py`, otherwise the bundled example project.

---

## 4. Find and watch your boards

Every board announces itself on the network once every few seconds. See them all with:

```bash
python tools/fleet.py
```

```text
==================================================================================
  UNTETHERED ACTIVE FLEET MONITOR
==================================================================================
DEVICE ID        IP ADDRESS       VER      APP HASH   SYS HASH   STATUS     SEEN
----------------------------------------------------------------------------------
workshop-pico    192.168.1.101    1.4.0    4468bd6    b2c41a9    RUNNING    1s ago
garden-sensor    192.168.1.105    1.3.0    9c1e2f0    b2c41a9    CRASHED    2s ago
----------------------------------------------------------------------------------
```

What the status means:

| Status | Meaning |
| :--- | :--- |
| `RUNNING` | Your program is running. |
| `CRASHED` | Your program stopped with an error. The board is still online: look at the error in the REPL and send a fix. |
| `STOPPED` | Your program finished on its own. |
| `STANDBY` | The app was wiped; the board is waiting for an update. |
| `OFFLINE` | No announcement for a while (shown by the monitor, not sent by the board). |

The IP address is also printed on the USB serial console when the board connects to Wi-Fi.

---

## 5. The remote REPL (Telnet)

Connect to a board's Python prompt over the network with any Telnet client:

* **Windows:** [PuTTY](https://www.putty.org/): enter the board's IP, select connection type *Other → Telnet*, port 23. (The built-in `telnet` command must first be enabled under *Windows Features → Telnet Client*.)
* **macOS / Linux:** `telnet 192.168.1.101 23` (on newer macOS install it with `brew install telnet`), or `nc 192.168.1.101 23`.

Enter your Telnet password and you'll see:

```text
====================================================
  Untethered Remote REPL [workshop-pico]
  IP: 192.168.1.101 | Status: CRASHED | Version: 1.4.0
  Escape character is Ctrl-] or close socket.
====================================================
```

**What you can do depends on your program:**

* **While your program is running**, the session is a live view of its output (everything it `print`s), but it can't take commands and Ctrl+C does not stop the program.
* **Once your program has crashed or finished**, you get the normal `>>>` prompt:

  ```python
  >>> import os
  >>> os.listdir()
  ['boot.py', 'config.py', 'lib', 'main.py', 'version.json']
  >>> import machine
  >>> machine.reset()        # restart the board
  ```

To close the session, close the window (PuTTY) or press **Ctrl+]** and type `quit` (telnet).

> [!WARNING]
> **The REPL is a full Python shell: whoever gets in controls the board.** Always set `TELNET_PASSWORD`. Telnet is not encrypted, so the password keeps casual users out but not someone who can listen to your network traffic. If you don't need the REPL, turn it off with `ENABLE_TELNET = False`.
>
> From the REPL, anyone can read the update key in `config.py` and use it to send updates or wipe commands to **every** board that shares that key. That's why a board with `OTA_SECRET_KEY` but no `TELNET_PASSWORD` turns the REPL off by itself. After a wrong password, the board waits before it accepts the next login (1 s, doubling up to 60 s).

> **Not sure whether to keep the REPL on?** Keep it on while you're still building and fixing your project. Once a board is finished and just does its job (especially on a network shared with other people), turn it off. Updates keep working without it.

---

## 6. Troubleshooting

**Watch the board's own messages first.** Plug it into USB and run `mpremote resume repl`. You'll see what the board prints, including Wi-Fi status, update progress and error messages. (Use `resume`: plain `mpremote` commands soft-reset the board and may fail with *"could not enter raw repl"* while the network services are printing.)

| Problem | What to check |
| :--- | :--- |
| Board doesn't join Wi-Fi | The network must be 2.4 GHz. Check the name and password in `config.py`. The board keeps retrying in the background, so it reconnects once the network is back. |
| `deploy.py` runs but boards don't update | **Firewall:** allow Python on private networks (Windows asks the first time; if you clicked "Cancel", allow it under *Windows Security → Firewall → Allow an app*). It needs TCP port 8000 and UDP port 8266. Also check that the board's `OTA_MANIFEST_URL` still matches the *Manifest URL* printed by `deploy.py`; your PC's IP may have changed. To change it, connect the board over USB and run the Step 4 `provision(...)` command again with the new URL. |
| Board prints `SECURITY ERROR ... Signature mismatch` | The update key given to `deploy.py` differs from `OTA_SECRET_KEY` on the board. |
| Board prints `Manifest is older than the installed one` | The board has already installed a newer update. Just deploy again. If you deploy from more than one PC, their clocks must be roughly right: update numbers come from the clock, so a PC whose clock is behind is rejected until it catches up. |
| One board didn't update, the others did | It was probably off or still starting when `deploy.py` sent the notification. Run `deploy.py` again. Boards that are often off can check by themselves: set `OTA_CHECK_INTERVAL = 300`. |
| Telnet connection refused, board prints `Telnet REPL disabled` | The board has an update key but no Telnet password. Set `TELNET_PASSWORD` in `config.py` (see [the REPL warning](#5-the-remote-repl-telnet)). |
| Board prints `Push ignored: unsigned board without OTA_MANIFEST_URL` | Set `OTA_MANIFEST_URL` (and ideally `OTA_SECRET_KEY`) on the board. See [Security](#8-security). |
| `fleet.py` shows no boards | Same firewall rule as above (UDP 8266). Guest networks and some mesh systems block devices from seeing each other. |
| Board rejects the library update (`does not load on this board`) | Your MicroPython is too old for the precompiled `untethered.mpy`. Update MicroPython, or copy `dist/untethered.py` to the board's `lib/` folder instead. |
| Nothing works over Wi-Fi at all | Connect over USB. A broken `config.py` or `boot.py` can only be fixed there. |

---

## 7. Settings reference (`config.py`)

`provision()` writes the essential settings for you. To change or add settings, edit `config.py` on the board (a full template is in [`config.example.py`](config.example.py)) and restart it.

| Setting | Default | What it does | Not sure? Use this |
| :--- | :--- | :--- | :--- |
| `WIFI_SSID`, `WIFI_PASSWORD` | – | Your Wi-Fi network. Leave the password empty for open networks. | Your 2.4 GHz network |
| `WIFI_COUNTRY` | `"DE"` | Two-letter country code for Wi-Fi regulations, e.g. `"US"`, `"GB"`. | The country you're in |
| `DEVICE_NAME` | `"pico-w"` | The board's name in the fleet monitor and for `--target`. Keep it unique. | Where it is or what it does: `garage-door`, `greenhouse-temp` |
| `OTA_MANIFEST_URL` | `None` | Where the board looks for updates (the *Manifest URL* from `deploy.py`). | Always set it |
| `OTA_SECRET_KEY` | `None` | Update key. When set, the board only accepts updates and wipe commands signed with it. | Always set it |
| `OTA_CHECK_INTERVAL` | `0` | Also check for updates every N seconds (0 = only when `deploy.py` notifies the board). | `0`; `300` for boards that are sometimes off |
| `TELNET_PASSWORD` | `None` | Password for the remote REPL. Required when `OTA_SECRET_KEY` is set, or the REPL stays off. | Always set it |
| `ENABLE_TELNET` | `True` | Set to `False` to turn the remote REPL off. | `True` while building, `False` once finished |
| `TELNET_PORT` | `23` | Port of the remote REPL. | Leave it |
| `BEACON_PORT` | `8266` | UDP port for announcements and update notifications (must match the PC tools). | Leave it |
| `BEACON_INTERVAL` | `5` | Seconds between announcements. | Leave it |
| `WATCHDOG_TIMEOUT_MS` | `0` | Restart the board automatically if the network services freeze (max `8388`; 0 = off). It does not watch your own program. | `0` while building; `8000` for boards that must run unattended |
| `OTA_ALLOW_BOOT_UPDATE` | `False` | Allow updates to replace `boot.py` (see [below](#10-how-it-works)). | `False` |
| `OTA_ALLOW_UNSIGNED_PUSH` | `False` | See [Security](#8-security). Only for trusted test networks. | `False` |

Why the watchdog is off while building: once started it can't be stopped. After a soft reset (which `mpremote` does before copying files over USB) the network services stop feeding it, so the board restarts a few seconds later and cuts off whatever you were doing.

Everything can also be passed to `untethered.start(...)` directly, e.g. `untethered.start(telnet=False)`.

---

## 8. Security

Untethered can install code on your boards over the network, so it matters who else can do that.

**With an update key (`OTA_SECRET_KEY` on the board, `--secret` on the PC)**, boards only accept updates signed with that key:

* every file is checked against the signed update, so nothing can be swapped in transit;
* an old update can't be replayed to downgrade a board;
* remote wipe commands must be signed too, and each one works only once.

**Without an update key**, anyone on your network could send code to your boards. As a safety net, an unsigned board only downloads from its own `OTA_MANIFEST_URL` and ignores remote wipes. A board with neither a key nor a manifest URL ignores update notifications entirely, unless you set `OTA_ALLOW_UNSIGNED_PUSH = True`. That setting is meant for quick experiments on a network you fully trust: it lets *anyone* on the network install code on the board.

The remote REPL is protected by `TELNET_PASSWORD`, but Telnet itself is unencrypted (see [the warning above](#5-the-remote-repl-telnet)). **The update key is only as safe as the REPL:** anyone who gets into the REPL (or holds a board) can read the key, and all boards that share it trust whatever it signs. To limit the damage, give groups of boards different keys, and turn the REPL off on finished boards. Update notifications themselves are not signed, so a board handles at most one every 10 seconds. Updates are sent over plain HTTP: the update key guarantees they are *authentic*, not that they are *private*.

The signing uses HMAC-SHA256 (RFC 2104) with timing-safe comparison and needs no extra packages.

---

## 9. Wiping and resetting boards

| Scope | What gets removed | Afterwards | Still reachable over Wi-Fi? |
| :--- | :--- | :--- | :---: |
| **`app`** | Your program files (`main.py`, etc.) | **Standby**: the board waits for the next update | Yes |
| **`system`** | Libraries in `lib/`, except Untethered itself | Your program may be missing its libraries until the next update | Yes |
| **`all`** | Everything on the board, including `config.py`, `boot.py` and Untethered (only the replay-protection counter stays) | Plain MicroPython, as after Step 1 of setup | **No**: set it up again over USB ([section 2](#2-set-up-a-board-about-10-minutes), from Step 4) |

> **Not sure which one?** `app` when you want to start a new project on the board. `system` only when a library in `lib/` is causing trouble. `all` only when you're giving the board away or want to start completely fresh, and only if you can connect it to USB afterwards.

**From your PC** (requires the update key; boards without one ignore wipe commands):

```bash
python tools/deploy.py --wipe app --target workshop-pico   # one board
python tools/deploy.py --wipe app                          # all boards
python tools/deploy.py --wipe all --target workshop-pico   # factory reset (asks first)
```

**From the REPL** or your own code:

```python
import untethered
untethered.wipe("app")     # remove program, restart into standby
untethered.wipe("system")  # remove lib/ libraries, keep Untethered
untethered.wipe("all")     # factory reset
```

---

## 10. How it works

This section is for the curious. You don't need it to use Untethered.

* **Two cores.** Wi-Fi, the Telnet REPL, announcements and updates run in a background thread on the RP2040's second core (Core 1). Your program runs on Core 0 under `@untethered.app`. When your program crashes, the error is recorded (status `CRASHED`), shown on USB serial and in any open Telnet session, and Core 0 drops into the MicroPython REPL while Core 1 keeps the board reachable.
* **Why `boot.py`.** MicroPython runs `boot.py` before it even reads `main.py`. Starting the network services there means that a `main.py` which can't be loaded at all (a syntax error, a missing import) still leaves the board online. `deploy.py` does not send `boot.py` by default and updates never delete it, because a broken `boot.py` is the one thing that takes a board offline. To ship one anyway, use `deploy.py --include-boot` *and* set `OTA_ALLOW_BOOT_UPDATE = True` on the board.
* **Differential updates.** `deploy.py` computes a SHA-256 hash of every file and writes a `manifest.json`. Files are grouped into two components: `system` (everything under `lib/`, including Untethered itself) and `app` (everything else). A combined hash per component lets a board see at a glance whether anything changed. It then downloads only the files whose hash differs, verifies each one while downloading, and removes files that disappeared from your project.
* **Power-loss safe.** Downloads go to temporary `.ota_new` files. Only once everything has been verified does the board write a journal and swap the files in. If the power fails during the swap, the board finishes the job at the next start.
* **Safe library updates.** Before replacing Untethered itself, the board test-imports the new version and rejects the update if it doesn't load (for example an `.mpy` built for a different MicroPython version). When switching between `untethered.py` and `untethered.mpy`, the old one is removed so it can't shadow the new one.
* **Nothing hangs forever.** Network requests time out after 10 seconds, and a Telnet login is collected piece by piece, so a stalled download or an idle login never blocks announcements or updates. After a failed login, the next attempt is delayed by a second.
* **Announcements and notifications.** Boards broadcast a small UDP message (name, IP, version, component hashes, status) on port 8266. `deploy.py` sends update notifications to the same port, to all boards or to one `--target`.
* **Wi-Fi.** The board reconnects automatically after a dropout (retrying every 10 s, backing off to 5 min). During a Telnet session, Wi-Fi power saving is turned off so typing feels instant; it is turned back on afterwards.
* **Only listed files are served.** `deploy.py`'s web server serves exactly the files in the manifest, so nothing else on your PC (such as a `config.py` with your secrets) can be downloaded. `config.py`, `version.json` and the board's internal state files are never sent to or overwritten on a board.

---

## 11. Known limitations

* **No typing into the REPL while your program runs.** On the Pico W, Telnet input only reaches the Python prompt once your program has stopped. Ctrl+C over Telnet does not interrupt it.
* **Networking runs in a thread on Core 1.** MicroPython's threading on the RP2040 is still marked experimental, and its network stack is not fully thread-safe. Test long-running installations well.
* **The watchdog only guards the network services.** `WATCHDOG_TIMEOUT_MS` restarts the board if the background services or an update freeze, not if your own program hangs without crashing.
* **Some mistakes still need USB:** a broken `config.py` or `boot.py`, or a full `wipe("all")`.
* **Upgrading to v2.1.0:** a board with `OTA_SECRET_KEY` but no `TELNET_PASSWORD` now turns its remote REPL off (an open REPL would expose the update key). Updates keep working; set `TELNET_PASSWORD` to get the REPL back.
* **Upgrading from v1.0.0:** signed updates now include a sequence number, so boards on v1.0.0 with `OTA_SECRET_KEY` set reject new signed updates. Update those boards once over USB. Unsigned boards update normally; afterwards, unsigned boards without an `OTA_MANIFEST_URL` need `OTA_ALLOW_UNSIGNED_PUSH = True` to keep following update notifications.

---

## 12. Development

### Repository layout

```text
Untethered/
├── src/untethered/        # Source fragments of ONE module, concatenated in order by
│                          #   tools/bundle.py (they share globals; not importable on their own)
│   ├── core.py            # Runtime state, start(), provision(), Wi-Fi
│   ├── crypto.py          # HMAC-SHA256 & timing-safe compare
│   ├── telnet.py          # Remote REPL (os.dupterm) and non-blocking login
│   ├── ota.py             # Differential update engine, journal, wipe
│   ├── beacon.py          # Core 1 background loop: announcements, notifications, Telnet
│   └── supervisor.py      # @untethered.app crash guard
├── dist/                  # Built bundles (committed: mip installs straight from here)
│   ├── untethered.py      # Single-file source bundle
│   └── untethered.mpy     # Precompiled bundle (~17 KB)
├── examples/simple_project/
├── tests/                 # Host-side tests
├── tools/
│   ├── bundle.py          # Builds dist/ from src/
│   ├── deploy.py          # Manifest, update server, notifications, wipe
│   └── fleet.py           # Fleet monitor
├── config.example.py      # Settings template
└── package.json           # mip package spec; "version" is the single source of truth
```

### Building and testing

```bash
pip install mpy-cross==1.29.0.post2   # pinned: the .mpy format must match board firmware
python tools/bundle.py                 # rebuilds dist/untethered.py and dist/untethered.mpy
python -m unittest discover tests      # runs on your PC with a simulated board
```

CI checks that the committed `dist/` matches a fresh build, so run `bundle.py` and commit `dist/` together with source changes. To release, bump `version` in `package.json` and push a matching `vX.Y.Z` tag; the release workflow builds, tests and publishes the bundles.
