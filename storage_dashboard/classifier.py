"""Conservative file classification for the storage dashboard."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

CLASS_SAFE_TO_REVIEW = "safe_to_review"
CLASS_CAUTION = "caution"
CLASS_DO_NOT_TOUCH = "do_not_touch"
UNKNOWN_CATEGORY = CLASS_DO_NOT_TOUCH

RISK_LOW = "low"
RISK_MEDIUM = "medium"
RISK_HIGH = "high"

SAFE_DOWNLOAD_EXTENSIONS = frozenset({".dmg", ".zip", ".pkg", ".iso"})
SAFE_BUILD_OUTPUT_NAMES = frozenset(
    {"dist", "build", ".next", "target", ".pytest_cache", ".mypy_cache", "__pycache__"}
)
SAFE_DEPENDENCY_NAMES = frozenset({"node_modules", ".venv", "venv"})
CAUTION_LIBRARY_NAMES = frozenset({"photos library.photoslibrary", "music library.musiclibrary"})
CAUTION_SYNC_NAMES = frozenset({"icloud drive", "dropbox", "google drive", "onedrive"})
CAUTION_VM_EXTENSIONS = frozenset({".vmwarevm", ".pvm", ".vbox", ".vdi", ".vmdk"})
CAUTION_DATABASE_EXTENSIONS = frozenset({".db", ".sqlite", ".sqlite3", ".mdb"})
SOURCE_MARKERS = frozenset(
    {
        ".git",
        "pyproject.toml",
        "package.json",
        "Cargo.toml",
        "go.mod",
        "pom.xml",
        "Makefile",
    }
)
APP_SUPPORT_PARTS = ("library", "application support")
RISKY_ROOTS = frozenset({"System", "bin", "sbin", "usr", "private", "Library", "Applications"})
OLD_DOWNLOAD_AGE = timedelta(days=30)


@dataclass(frozen=True)
class Classification:
    """Conservative cleanup review classification."""

    classification: str
    reason: str
    risk: str


def classify_path(
    path: str | Path,
    *,
    root: str | Path | None = None,
    is_dir: bool | None = None,
    size_bytes: int | None = None,
    modified_at: datetime | None = None,
    source_marker_present: bool = False,
    denied: bool = False,
) -> Classification:
    """Classify a path using conservative local-only rules.

    Unknown paths intentionally default to do_not_touch.
    """

    target = Path(path).expanduser()
    root_path = Path(root).expanduser() if root is not None else None
    name = target.name
    lower_name = name.lower()
    suffix = target.suffix.lower()
    parts = tuple(part.lower() for part in target.parts)

    if denied:
        return Classification(CLASS_DO_NOT_TOUCH, "denied or system root", RISK_HIGH)

    if target.is_absolute() and len(target.parts) > 1 and target.parts[1] in RISKY_ROOTS:
        return Classification(CLASS_DO_NOT_TOUCH, "system or application root", RISK_HIGH)

    if any(part in {"backups.backupdb", ".timemachine", "time machine backups"} for part in parts):
        return Classification(CLASS_DO_NOT_TOUCH, "backup data", RISK_HIGH)

    if any(part in {"homebrew", ".brew", ".cargo", ".rustup", ".npm"} for part in parts):
        return Classification(CLASS_DO_NOT_TOUCH, "package manager internal data", RISK_HIGH)

    if ".cache" in parts and "pip" in parts:
        return Classification(CLASS_DO_NOT_TOUCH, "package manager internal data", RISK_HIGH)

    if lower_name in CAUTION_LIBRARY_NAMES or suffix in {".photoslibrary", ".musiclibrary"}:
        return Classification(CLASS_CAUTION, "photo or music library", RISK_MEDIUM)

    if lower_name in CAUTION_SYNC_NAMES or any(part in CAUTION_SYNC_NAMES for part in parts):
        return Classification(CLASS_CAUTION, "cloud sync folder", RISK_MEDIUM)

    if suffix in CAUTION_VM_EXTENSIONS:
        return Classification(CLASS_CAUTION, "virtual machine data", RISK_MEDIUM)

    if suffix in CAUTION_DATABASE_EXTENSIONS:
        return Classification(CLASS_CAUTION, "database file", RISK_MEDIUM)

    if APP_SUPPORT_PARTS[0] in parts and APP_SUPPORT_PARTS[1] in parts:
        return Classification(CLASS_CAUTION, "application support data", RISK_MEDIUM)

    if is_dir and lower_name in SAFE_BUILD_OUTPUT_NAMES:
        return Classification(CLASS_SAFE_TO_REVIEW, "build output folder", RISK_LOW)

    if is_dir and lower_name in SAFE_DEPENDENCY_NAMES:
        return Classification(CLASS_SAFE_TO_REVIEW, "dependency folder", RISK_LOW)

    if name.startswith("."):
        return Classification(CLASS_DO_NOT_TOUCH, "unknown hidden path", RISK_HIGH)

    if is_dir and source_marker_present:
        return Classification(CLASS_CAUTION, "source code project", RISK_MEDIUM)

    if _is_in_downloads(target, root_path):
        if not is_dir and suffix in SAFE_DOWNLOAD_EXTENSIONS:
            if _is_older_than(modified_at, OLD_DOWNLOAD_AGE):
                return Classification(CLASS_SAFE_TO_REVIEW, "old downloaded installer/archive", RISK_LOW)
            return Classification(CLASS_CAUTION, "recent downloaded installer/archive", RISK_MEDIUM)
        if not is_dir and size_bytes is not None and size_bytes >= 500 * 1024 * 1024:
            return Classification(CLASS_SAFE_TO_REVIEW, "large file in Downloads", RISK_LOW)

    return Classification(CLASS_DO_NOT_TOUCH, "unclassified", RISK_HIGH)


def _is_in_downloads(path: Path, root: Path | None) -> bool:
    if root is not None and root.name == "Downloads":
        return True
    return "Downloads" in path.parts


def _is_older_than(modified_at: datetime | None, age: timedelta) -> bool:
    if modified_at is None:
        return False
    if modified_at.tzinfo is None:
        modified_at = modified_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - modified_at.astimezone(timezone.utc) > age
