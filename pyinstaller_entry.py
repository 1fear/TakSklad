import sys

from taksklad.main import run_app, run_gui_smoke


if __name__ == "__main__":
    if "--smoke-import" in sys.argv:
        print("TakSklad import OK")
        raise SystemExit(0)
    if "--smoke-gui" in sys.argv:
        raise SystemExit(run_gui_smoke())
    sys.exit(run_app())
