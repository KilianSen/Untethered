"""
Untethered Supervisor: Anti-Bricking Crash Guard & Decorator Runner on Core 0
"""
import sys


def _handle_supervised_crash(exc):
    global _device_status, _last_error
    _device_status = "CRASHED"
    _last_error = str(exc)
    err_msg = (
        f"\r\n\033[1;31m====================================================\033[0m\r\n"
        f"\033[1;31m  CRITICAL: User application crashed on Core 0!\033[0m\r\n"
        f"  Error: {exc}\r\n"
        f"\033[1;33m  System remains active in SAFE RECOVERY MODE.\033[0m\r\n"
        f"  Wi-Fi, OTA updates and the Telnet REPL stay available.\r\n"
        f"\033[1;31m====================================================\033[0m\r\n\r\n"
    )
    print(err_msg)
    try:
        sys.print_exception(exc)
    except Exception:
        pass
    if _active_telnet_stream:
        try:
            _active_telnet_stream.write(err_msg.encode("utf-8"))
        except Exception:
            pass


def supervise(target=None, **kwargs):
    """
    Supervises user application execution on Core 0 with anti-bricking crash guard.
    Supports usage as a decorator:
        @untethered.app(ssid="...", password="...")
        def main(): ...
    Or directly:
        @untethered.app
        def main(): ...
    Or function call:
        untethered.run(my_main_function)
    """
    def decorator(func):
        global _device_status
        if not _initialized:
            start(**kwargs)

        _device_status = "RUNNING"
        func_name = getattr(func, "__name__", "user_code")
        print(f"[Untethered] Launching supervised user app '{func_name}' on Core 0...")

        try:
            import uasyncio as asyncio
        except ImportError:
            try:
                import asyncio
            except ImportError:
                asyncio = None

        try:
            result = func()
            # Calling an async def returns a coroutine (a generator on MicroPython, where
            # functions have no __code__ to inspect), so detect it by what came back
            if hasattr(result, "send") and hasattr(result, "throw"):
                if asyncio:
                    asyncio.run(result)
                else:
                    print("[Untethered] ERROR: async app needs asyncio, which is unavailable.")
        except Exception as exc:
            _handle_supervised_crash(exc)
        else:
            _device_status = "STOPPED"
            print(f"[Untethered] App '{func_name}' finished.")

        # Return instead of idling: when main.py ends, Core 0 drops into the MicroPython REPL,
        # so the Telnet (and USB) REPL accepts commands again. Core 1 services keep running.
        return func

    if target is not None:
        if callable(target):
            return decorator(target)
        else:
            raise TypeError("Target must be callable")
    return decorator


app = supervise
run = supervise
