# Untethered Configuration Template (RP2040-W / Pico W)
# Copy this file to 'config.py' and adjust settings for your network.

# WiFi Settings
WIFI_SSID = "YOUR_WIFI_SSID"
WIFI_PASSWORD = "YOUR_WIFI_PASSWORD"
WIFI_COUNTRY = "DE"  # ISO 3166-1 alpha-2 country code (e.g. 'DE', 'US', 'GB')

# Unique identifier for this device in your fleet
DEVICE_NAME = "pico-w-01"

# Remote REPL (Telnet) Configuration
ENABLE_TELNET = True   # Set to False to completely disable the wireless Telnet REPL
TELNET_PORT = 23
TELNET_PASSWORD = None # Strongly recommended. Telnet is plaintext: this keeps casual LAN users out,
                       # but anyone who can sniff the network can read it.

# Fleet Discovery and Push-Deploy UDP Port
BEACON_PORT = 8266
BEACON_INTERVAL = 5    # Heartbeat interval in seconds

# OTA Server URL (Points to tools/deploy.py on your host PC)
OTA_MANIFEST_URL = "http://192.168.1.100:8000/manifest.json"

# OTA Polling Interval (in seconds, 0 to disable periodic polling and rely on push triggers)
OTA_CHECK_INTERVAL = 300

# Cryptographic Signing Key (HMAC-SHA256) - strongly recommended
# When set, devices reject any update not signed with this exact key, reject replayed or
# older manifests, and accept signed remote wipe commands.
# When None, anyone on the network can push updates and remote wipe is disabled.
OTA_SECRET_KEY = None

# Unsigned boards (OTA_SECRET_KEY = None) without an OTA_MANIFEST_URL ignore push triggers,
# because a push can name any server. Set True only on a trusted development LAN: it lets
# anyone on the network install code on this board.
OTA_ALLOW_UNSIGNED_PUSH = False

# Accept boot.py from OTA updates (deploy with --include-boot). Off by default: boot.py starts
# Wi-Fi and OTA, so a broken one can only be fixed over USB.
OTA_ALLOW_BOOT_UPDATE = False

# Hardware Watchdog Timer Timeout (0 to disable during development; RP2040 max is 8388 ms)
# Fed by the Core 1 network daemon: it recovers a hung daemon or stalled update,
# but does NOT detect a hung user application on Core 0.
WATCHDOG_TIMEOUT_MS = 0
