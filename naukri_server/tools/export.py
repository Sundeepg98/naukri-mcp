"""Export data — dump applications, saved jobs, or search results to JSON/CSV.

Tool layer is now a thin orchestrator: data loading + file writing live here
(file I/O can't move because tests patch tools.export._EXPORTS_DIR / Path).
Pure helpers for arg validation, path resolution, and CSV serialisation live
in services/export_service.py.
"""

import json
from pathlib import Path
from typing import Optional

from naukri_server.config import logger, EXPORTS_DIR

# Re-export from service layer for backward compatibility
from naukri_server.services.sync_service import _flatten_for_csv  # noqa: F401
from naukri_server.services.export_service import (
    validate_export_args,
    resolve_export_path,
    collect_csv_headers,
    render_csv,
)

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
    err = validate_export_args(data_type, format)
    if err:
        return err
    format = format.lower()
    data_type = data_type.lower()

    # Load data
    records: list = []
    if data_type == "applications":
        from naukri_server.database import list_all_applications
        records = await list_all_applications()
        if not records:
            return {"status": "error", "message": "No applications data found. Run naukri_sync_applications() first.", "error_code": "NOT_FOUND"}

    elif data_type == "saved_jobs":
        from naukri_server.database import list_all_saved_jobs
        records = await list_all_saved_jobs()
        if not records:
            return {"status": "error", "message": "No saved jobs data found. Run naukri_sync_saved() first.", "error_code": "NOT_FOUND"}

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
    file_path, path_err = resolve_export_path(_EXPORTS_DIR, data_type, format, output_path)
    if path_err:
        return path_err
    # Type narrow for mypy/pyright (file_path is non-None when path_err is None)
    assert file_path is not None

    # Ensure the resolved file's parent dir exists. _EXPORTS_DIR.mkdir above only
    # covers the default dir; a custom output_path may point at a (verbatim) path
    # whose parent doesn't exist yet, which would make write_text raise
    # FileNotFoundError on a clean machine.
    file_path.parent.mkdir(parents=True, exist_ok=True)

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
            headers = collect_csv_headers(flat)
            file_path.write_text(render_csv(flat, headers), encoding="utf-8")

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
