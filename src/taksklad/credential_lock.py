"""Stable current-user cross-process lock for DPAPI store mutations."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile

from .secret_store import SecretStoreUnavailable
from .single_instance import acquire_single_instance_lock, release_single_instance_lock


def credential_mutation_lock_directory() -> Path:
    root = str(os.environ.get("LOCALAPPDATA") or "").strip()
    if os.name == "nt" and not root:
        raise SecretStoreUnavailable("LOCALAPPDATA is unavailable")
    if not root:
        mode = str(os.environ.get("TAKSKLAD_SECRET_STORE_MODE") or "").strip().casefold()
        if os.name != "nt" and not getattr(sys, "frozen", False) and mode in {"development", "test"}:
            return Path(tempfile.gettempdir()) / f"TakSklad-dev-{os.geteuid()}" / "secrets"
        raise SecretStoreUnavailable("credential mutation lock is Windows-only")
    return Path(root) / "TakSklad" / "secrets"


def acquire_credential_mutation_lock(*, process_running_func=None):
    directory = credential_mutation_lock_directory()
    directory.mkdir(parents=True, exist_ok=True)
    if process_running_func is not None:
        return acquire_single_instance_lock(
            app_dir=str(directory),
            process_running_func=process_running_func,
        )
    return acquire_single_instance_lock(app_dir=str(directory))


def release_credential_mutation_lock(lock):
    return release_single_instance_lock(lock)
