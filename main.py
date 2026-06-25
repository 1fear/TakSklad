import os
import sys


ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(ROOT_DIR, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from taksklad.main import run_app, run_gui_smoke  # noqa: E402


if __name__ == "__main__":
    if "--smoke-import" in sys.argv:
        print("TakSklad import OK")
        raise SystemExit(0)
    if "--smoke-gui" in sys.argv:
        raise SystemExit(run_gui_smoke())
    run_app()
