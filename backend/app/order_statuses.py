"""Order status constants shared without importing order persistence services."""

STATUS_COMPLETED = "completed"
STATUS_NOT_COMPLETED = "not_completed"
STATUS_RETURNED = "returned"
STATUS_ARCHIVED_NO_KIZ = "archived_no_kiz"
STATUS_CANCELLED = "cancelled"
STATUS_REMOVED_FROM_GOOGLE = "removed_from_google_sheet"
COMPLETED_STATUSES = (STATUS_COMPLETED, "done", "closed", STATUS_RETURNED)
INACTIVE_ORDER_STATUSES = (*COMPLETED_STATUSES, STATUS_ARCHIVED_NO_KIZ, STATUS_CANCELLED)
HIDDEN_ITEM_STATUSES = (STATUS_REMOVED_FROM_GOOGLE,)
