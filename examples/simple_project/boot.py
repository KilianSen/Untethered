# examples/simple_project/boot.py
# Starts Wi-Fi, the Telnet REPL and OTA before main.py runs, so even a main.py with a
# syntax error cannot take the board offline. Settings come from config.py.
import untethered

untethered.start()
