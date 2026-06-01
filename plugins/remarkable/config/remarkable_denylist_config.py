"""reMarkable tablet privacy config.

Controls what the chassis remarkable plugin is allowed to read from the tablet.

Model: default-allow, denylist-enforced at the sync boundary.

HARD_DENY_FOLDERS are absolutely excluded - no read, no cache, no index.
SUSPICIOUS_FOLDER_KEYWORDS match folder NAMES (not document titles) and flag
the folder for the installer's approval before anything inside it is read.

Edit these lists at install time to match the installer's tablet layout.
The module is re-imported on every sync run so changes take effect immediately.
"""

HARD_DENY_FOLDERS: tuple[str, ...] = (
    "Private",
    "Non-indexed Books",
)

# Matched against folder NAMES exactly (case-insensitive). A document TITLED
# "Journal of a CEO" in a normal Business Books folder is unaffected - only a
# folder literally named e.g. "Journal" or "Diary" triggers the needs_approval
# flow.
SUSPICIOUS_FOLDER_KEYWORDS: tuple[str, ...] = (
    "diary",
    "journal",
    "private",
    "personal",
    "therapy",
)
