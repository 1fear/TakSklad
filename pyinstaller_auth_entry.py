"""Console-mode PyInstaller entrypoint for current-user desktop auth operations."""

from taksklad.cli import dispatch_auth_helper_cli


if __name__ == "__main__":
    raise SystemExit(dispatch_auth_helper_cli())
