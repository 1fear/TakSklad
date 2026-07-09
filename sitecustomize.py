import os
import sys


ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(ROOT_DIR, "src")
if os.path.isdir(SRC_DIR) and SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

for brew_prefix in ("/opt/homebrew", "/usr/local"):
    python_tk_libexec = os.path.join(
        brew_prefix,
        "opt",
        f"python-tk@{sys.version_info.major}.{sys.version_info.minor}",
        "libexec",
    )
    if os.path.isdir(python_tk_libexec) and python_tk_libexec not in sys.path:
        sys.path.append(python_tk_libexec)
