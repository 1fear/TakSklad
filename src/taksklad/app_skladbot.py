import tkinter as tk

from .config import SKLADBOT_SYNC_INTERVAL_MS


class SkladBotActionsMixin:
    """SkladBot synchronization is orchestrated by backend /sync/sources."""

    def run_skladbot_periodic_refresh(self):
        try:
            if (
                not self.update_required
                and not self.operation_in_progress
                and not self.refresh_in_progress
                and not self.current_order
            ):
                self.refresh_from_sheet()
        finally:
            try:
                self.after(SKLADBOT_SYNC_INTERVAL_MS, self.run_skladbot_periodic_refresh)
            except tk.TclError:
                pass

    def sync_skladbot_async(self):
        if not self.refresh_in_progress and not self.operation_in_progress and not self.current_order:
            self.refresh_from_sheet()
