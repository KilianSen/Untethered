# examples/simple_project/main.py
# Your program. @untethered.app runs it and, if it crashes, keeps Wi-Fi, OTA and the
# Telnet REPL available so you can look at the error and push a fix.
import time
import untethered


@untethered.app
def main():
    print("Supervised user application running on Core 0...")
    counter = 0

    while True:
        counter += 1
        # Shows up on USB serial and in a Telnet session
        print(f"[Project] Iteration #{counter} | Device IP: {untethered.get_ip()}")
        time.sleep(2)
