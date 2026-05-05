"""Export service — pure helpers for CSV serialization and export-path validation.

Extracted from tools/export.py so the path-resolution and CSV-header logic can
be tested in isolation. File I/O and database loading remain in the tool because
test_export_deep.py patches tools.export._EXPORTS_DIR / Path against them.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

__all__ = [
    "VALID_EXPORT_TYPES",
    "VALID_EXPORT_FORMATS",
    "validate_export_args",
    "resolve_export_path",
    "collect_csv_headers",
    "render_csv",
]


VALID_EXPORT_TYPES: tuple[str, ...] = ("applications", "saved_jobs", "search_results")
VALID_EXPORT_FORMATS: tuple[str, ...] = ("json", "csv")


def validate_export_args(data_type: str, export_format: str) -> Optional[dict]:
    """Validate export arguments. Returns ``None`` if OK, else an error dict.

    Pure function — caller can short-circuit before any I/O.
    """
    fmt = export_format.lower()
    if fmt not in VALID_EXPORT_FORMATS:
        return {
            "status": "error",
            "message": f"Unsupported format '{export_format}'. Use 'json' or 'csv'.",
            "error_code": "VALIDATION_ERROR",
        }
    dt = data_type.lower()
    if dt not in VALID_EXPORT_TYPES:
        return {
            "status": "error",
            "message": f"Invalid data_type '{data_type}'. Use one of: {', '.join(VALID_EXPORT_TYPES)}",
            "error_code": "VALIDATION_ERROR",
        }
    return None


def resolve_export_path(
    exports_dir: Path,
    data_type: str,
    export_format: str,
    output_path: Optional[str] = None,
) -> tuple[Optional[Path], Optional[dict]]:
    """Resolve the final export file path under ``exports_dir``.

    Returns ``(path, None)`` on success or ``(None, error_dict)`` if the user
    supplied an out-of-tree ``output_path``.
    """
    if output_path:
        file_path = Path(output_path).resolve()
        exports_resolved = exports_dir.resolve()
        if not str(file_path).startswith(str(exports_resolved)):
            return None, {
                "status": "error",
                "message": "output_path must be within the exports/ directory",
                "error_code": "VALIDATION_ERROR",
            }
        return file_path, None

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return exports_dir / f"{data_type}_{date_str}.{export_format}", None


def collect_csv_headers(rows: list[dict]) -> list[str]:
    """Collect the union of keys across all rows, preserving insertion order.

    Used for CSV writer fieldnames so every column appears in the header even
    when individual rows are missing some fields.
    """
    seen: set = set()
    headers: list[str] = []
    for row in rows:
        for k in row:
            if k not in seen:
                headers.append(k)
                seen.add(k)
    return headers


def render_csv(rows: list[dict], headers: list[str]) -> str:
    """Render ``rows`` to a CSV string with the given header order.

    Uses ``extrasaction='ignore'`` so any extra keys in a row are silently
    dropped (matches the previous tool-layer behaviour).
    """
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=headers, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()
