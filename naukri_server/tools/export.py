"""Export data — dump applications, saved jobs, or search results to JSON/CSV."""

import csv
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from naukri_server import mcp
from naukri_server.config import logger

_PACKAGE_ROOT = Path(__file__).parent.parent.parent
_EXPORTS_DIR = _PACKAGE_ROOT / "exports"


def _flatten_for_csv(records: list[dict]) -> list[dict]:
    """Flatten nested dicts and lists for CSV output."""
    flat = []
    for rec in records:
        row = {}
        for k, v in rec.items():
            if isinstance(v, list):
                row[k] = ", ".join(str(i) for i in v)
            elif isinstance(v, dict):
                for sub_k, sub_v in v.items():
                    row[f"{k}.{sub_k}"] = sub_v if not isinstance(sub_v, (list, dict)) else str(sub_v)
            else:
                row[k] = v
        flat.append(row)
    return flat


@mcp.tool()
async def naukri_export_data(
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
        apps_file = _PACKAGE_ROOT / "applications.json"
        if not apps_file.exists():
            return {"status": "error", "message": "No applications data found. Run naukri_sync(entity=\"applications\") first.", "error_code": "NOT_FOUND"}
        try:
            records = json.loads(apps_file.read_text(encoding="utf-8"))
        except Exception as e:
            return {"status": "error", "message": f"Failed to read applications: {e}", "error_code": "API_ERROR"}

    elif data_type == "saved_jobs":
        saved_file = _PACKAGE_ROOT / "saved_jobs.json"
        if not saved_file.exists():
            return {"status": "error", "message": "No saved jobs data found. Run naukri_sync(entity=\"saved_jobs\") first.", "error_code": "NOT_FOUND"}
        try:
            records = json.loads(saved_file.read_text(encoding="utf-8"))
        except Exception as e:
            return {"status": "error", "message": f"Failed to read saved jobs: {e}", "error_code": "API_ERROR"}

    elif data_type == "search_results":
        if not keywords:
            return {"status": "error", "message": "keywords is required when data_type is 'search_results'.", "error_code": "VALIDATION_ERROR"}
        from naukri_server.tools.search import naukri_search_jobs
        result = await naukri_search_jobs(keywords=keywords, limit=50)
        if result.get("status") == "error":
            return result
        records = result.get("jobs", [])

    if not records:
        return {"status": "error", "message": f"No {data_type} records to export.", "error_code": "API_ERROR"}

    # Determine output path
    _EXPORTS_DIR.mkdir(exist_ok=True)
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ext = format
    if output_path:
        file_path = Path(output_path)
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
                return {"status": "error", "message": "No data to write after flattening.", "error_code": "API_ERROR"}
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
        return {"status": "error", "message": f"Failed to write {format} file: {e}", "error_code": "API_ERROR"}

    return {
        "status": "success",
        "file_path": str(file_path),
        "record_count": len(records),
        "data_type": data_type,
        "format": format,
    }
