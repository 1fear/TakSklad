import sys

from taksklad.main import run_app


if __name__ == "__main__":
    if "--smoke-import" in sys.argv:
        print("TakSklad import OK")
        raise SystemExit(0)
    sys.exit(run_app())
