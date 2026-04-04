"""Export data — dump applications, saved jobs, or search results to JSON/CSV."""

import csv
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from naukri_server.config import logger, EXPORTS_DIR

# Re-export from service layer for backward compatibility
from naukri_server.services.sync_service import _flatten_for_csv  # noqa: F401

_EXPORTS_DIR = EXPORTS_DIR


async def _export_data(
    data_type: str,
    format: str = "json",
    keywords: Optional[str] = None,
    output_path: Optional[str] = None,
) -> dict:
    """Export Naukri data to JSON or CSV file for external analysis.

    Exports applications, saved jobs, or live search results to a file.

    Args:
        data_type: What to export — "applications", "saved_jobs", or "search_results"
        format: Output format — "json" or "csv" (default "json")
        keywords: Required when data_type is "search_results" — search query
        output_path: Custom file path (default: exports/<type>_<date>.<ext>)

    Returns:
        - {status: "success", file_path, record_count, data_type}
        - {status: "error", message}
    """
    logger.info("Exporting %s as %s", data_type, format)
    format = format.lower()
    if format not in ("json", "csv"):
        return {"status": "error", "message": f"Unsupported format '{format}'. Use 'json' or 'csv'.", "error_code": "VALIDATION_ERROR"}

    data_type = data_type.lower()
    valid_types = ("applications", "saved_jobs", "search_results")
    if data_type not in valid_types:
        return {"status": "error", "message": f"Invalid data_type '{data_type}'. Use one of: {', '.join(valid_types)}", "error_code": "VALIDATION_ERROR"}

    # Load data
    records = []
    if data_type == "applications":
        from naukri_server.database import list_all_applications
        records = await list_all_applications()
        if not records:
            return {"status": "error", "message": "No applications data found. Run naukri_sync(entity=\"applications\") first.", "error_code": "NOT_FOUND"}

    elif data_type == "saved_jobs":
        from naukri_server.database import list_all_saved_jobs
        records = await list_all_saved_jobs()
        if not records:
            return {"status": "error", "message": "No saved jobs data found. Run naukri_sync(entity=\"saved_jobs\") first.", "error_code": "NOT_FOUND"}

    elif data_type == "search_results":
        if not keywords:
            return {"status": "error", "message": "keywords is required when data_type is 'search_results'.", "error_code": "VALIDATION_ERROR"}
        from naukri_server.tools.search import naukri_search_jobs
        result = await naukri_search_jobs(keywords=keywords, limit=50)
        if result.get("status") == "error":
            return result
        records = result.get("jobs", [])

    if not records:
        return {"status": "error", "message": f"No {data_type} records to export.", "error_code": "NOT_FOUND"}

    # Determine output path
    _EXPORTS_DIR.mkdir(exist_ok=True)
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ext = format
    if output_path:
        file_path = Path(output_path).resolve()
        exports_dir = _EXPORTS_DIR.resolve()
        if not str(file_path).startswith(str(exports_dir)):
            return {"status": "error", "message": "output_path must be within the exports/ directory", "error_code": "VALIDATION_ERROR"}
    else:
        file_path = _EXPORTS_DIR / f"{data_type}_{date_str}.{ext}"

    # Write file
    try:
        if format == "json":
            file_path.write_text(
                json.dumps(records, indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
        else:  # csv
            flat = _flatten_for_csv(records)
            if not flat:
                return {"status": "error", "message": "No data to write after flattening.", "error_code": "NOT_FOUND"}
            # Collect all keys across all rows for header
            all_keys = []
            seen = set()
            for row in flat:
                for k in row:
                    if k not in seen:
                        all_keys.append(k)
                        seen.add(k)
            output = io.StringIO()
            writer = csv.DictWriter(output, fieldnames=all_keys, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(flat)
            file_path.write_text(output.getvalue(), encoding="utf-8")

    except Exception as e:
        logger.error("Failed to write %s export file: %s", format, e)
        return {"status": "error", "message": f"Failed to write {format} file: {e}", "error_code": "API_ERROR"}

    return {
        "status": "success",
        "file_path": str(file_path),
        "record_count": len(records),
        "data_type": data_type,
        "format": format,
    }


# Public alias for backward compatibility (no longer an MCP tool — routed via naukri_sync)
naukri_export_data = _export_data
