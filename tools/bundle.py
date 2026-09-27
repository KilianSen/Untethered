"""
Untethered Bundle & Compiler CLI (tools/bundle.py)
- Combines modular source files in src/untethered/ into a single standalone module
- Generates dist/untethered.py and compiles it via mpy-cross into dist/untethered.mpy
- Injects the SemVer version from package.json (the single source of truth)

The files in src/untethered/ are fragments of ONE module, not a Python package: they share
globals and helpers with each other and only work once concatenated in MODULES_IN_ORDER.
"""
import argparse
import ast
import hashlib
import json
import os
import subprocess
import sys

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def package_version():
    with open(os.path.join(ROOT_DIR, "package.json"), encoding="utf-8") as f:
        return json.load(f)["version"]

def strip_module_docstring(lines):
    """Removes only the fragment's module docstring (found via ast, so no other code is touched)."""
    tree = ast.parse("".join(lines))
    first = tree.body[0] if tree.body else None
    if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)):
        return lines[:first.lineno - 1] + lines[first.end_lineno:]
    return lines


MODULES_IN_ORDER = [
    "core.py",
    "crypto.py",
    "telnet.py",
    "ota.py",
    "beacon.py",
    "supervisor.py",
]


def compute_sha256(filepath):
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while True:
            chunk = f.read(4096)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def bundle(version=None):
    version = version or package_version()
    src_dir = os.path.join(ROOT_DIR, "src", "untethered")
    dist_dir = os.path.join(ROOT_DIR, "dist")

    os.makedirs(dist_dir, exist_ok=True)

    header = f'''"""
Untethered: Wireless OTA & Remote REPL Library for Raspberry Pi Pico W
Version: {version}
Automated single-file bundle built from modular sources.
"""
__version__ = "{version}"
'''

    combined_lines = [header]

    for mod_name in MODULES_IN_ORDER:
        path = os.path.join(src_dir, mod_name)
        if not os.path.exists(path):
            raise FileNotFoundError(f"Missing module: {path}")

        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        filtered = strip_module_docstring(lines)
        # Fragments share one namespace, so relative imports between them are dropped
        filtered = [l for l in filtered if not l.lstrip().startswith(("from .", "import ."))]

        combined_lines.append(f"\n# ============================================================================\n")
        combined_lines.append(f"# Component: {mod_name}\n")
        combined_lines.append(f"# ============================================================================\n")
        combined_lines.extend(filtered)

    combined_content = "".join(combined_lines)

    # Always LF so the build is byte-identical on Windows and CI
    dist_py = os.path.join(dist_dir, "untethered.py")
    with open(dist_py, "w", encoding="utf-8", newline="\n") as f:
        f.write(combined_content)

    py_size = os.path.getsize(dist_py)
    print(f"[Bundle] Assembled single-file Python module: {dist_py} ({py_size:,} bytes)")

    # Compile with mpy-cross
    dist_mpy = os.path.join(dist_dir, "untethered.mpy")
    # -s: embed a fixed source name, not the absolute build path, so the .mpy is reproducible
    # (CI compares it byte for byte) and doesn't leak the builder's directory layout
    cmd = [sys.executable, "-m", "mpy_cross", "-s", "untethered.py", dist_py, "-o", dist_mpy]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print(f"[Error] mpy-cross failed:\n{res.stderr}")
        sys.exit(1)

    mpy_size = os.path.getsize(dist_mpy)
    reduction = (1.0 - (mpy_size / py_size)) * 100
    print(f"[Bundle] Compiled bytecode bundle: {dist_mpy} ({mpy_size:,} bytes | {reduction:.1f}% reduction)")
    print(f"         SHA-256: {compute_sha256(dist_mpy)}")
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Untethered Multi-Module Bundler and mpy-cross Compiler")
    parser.add_argument("--version", type=str, default=None,
                        help="Override the SemVer version (defaults to package.json)")
    args = parser.parse_args()
    bundle(version=args.version)
