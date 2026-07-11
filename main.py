from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup
from fastapi import FastAPI, File, HTTPException, UploadFile, status


app = FastAPI(
    title="COSC 3506 Course Registration API",
    version="1.0.0",
)

# Global catalog data is stored in FastAPI application state.
# The dictionary key is a normalized course code, such as COSC3506.
app.state.catalog: dict[str, dict[str, Any]] = {}


def normalize_course_code(course_code: str) -> str:
    """
    Convert differently formatted course codes into one internal format.

    Examples:
        COSC 3506  -> COSC3506
        COSC-3506  -> COSC3506
        cosc3506   -> COSC3506
    """
    return re.sub(r"[\s-]+", "", course_code).upper().strip()


def normalize_header(header: str) -> str:
    """
    Normalize an HTML table header so variations such as
    'Course Code' and 'Course-Code' can be compared consistently.
    """
    return re.sub(r"[^a-z0-9]", "", header.lower())


def parse_credits(value: str) -> int:
    """
    Safely convert a credit value into an integer.

    Examples:
        '3'   -> 3
        '3.0' -> 3
        ''    -> 0
    """
    match = re.search(r"-?\d+(?:\.\d+)?", value)

    if not match:
        return 0

    try:
        return int(float(match.group()))
    except ValueError:
        return 0


def extract_course_codes(text: str) -> list[str]:
    """
    Extract course codes from prerequisite or cross-listing text.

    Example:
        'Requires COSC 1046 and ITEC-1047'
        -> ['COSC 1046', 'ITEC-1047']
    """
    cleaned_text = text.strip()

    if not cleaned_text:
        return []

    if cleaned_text.lower() in {
        "none",
        "n/a",
        "na",
        "no prerequisite",
        "no prerequisites",
    }:
        return []

    pattern = r"\b[A-Za-z]{2,10}\s*[- ]?\s*\d{3,4}[A-Za-z]?\b"
    matches = re.findall(pattern, cleaned_text)

    results: list[str] = []
    seen: set[str] = set()

    for match in matches:
        displayed_code = re.sub(r"\s+", " ", match.strip())
        normalized_code = normalize_course_code(displayed_code)

        if normalized_code not in seen:
            seen.add(normalized_code)
            results.append(displayed_code)

    return results


def parse_catalog_html(html: str) -> dict[str, dict[str, Any]]:
    """
    Find a catalog table by examining its headers, then parse every
    course row into a normalized catalog dictionary.
    """
    soup = BeautifulSoup(html, "html.parser")
    parsed_catalog: dict[str, dict[str, Any]] = {}

    required_headers = {
        "coursecode",
        "title",
        "credits",
        "prerequisites",
        "crosslisted",
    }

    for table in soup.find_all("table"):
        rows = table.find_all("tr")

        if not rows:
            continue

        header_map: dict[str, int] | None = None
        header_row_position: int | None = None

        # Search for the header row instead of assuming it is always first.
        for row_position, row in enumerate(rows):
            cells = row.find_all(["th", "td"])
            headers = [
                normalize_header(cell.get_text(" ", strip=True))
                for cell in cells
            ]

            current_map = {
                header: index
                for index, header in enumerate(headers)
                if header
            }

            if required_headers.issubset(current_map.keys()):
                header_map = current_map
                header_row_position = row_position
                break

        if header_map is None or header_row_position is None:
            continue

        for row in rows[header_row_position + 1 :]:
            cells = row.find_all(["td", "th"])

            if not cells:
                continue

            values = [cell.get_text(" ", strip=True) for cell in cells]

            highest_required_index = max(header_map.values())

            if len(values) <= highest_required_index:
                continue

            course_code = values[header_map["coursecode"]].strip()
            title = values[header_map["title"]].strip()
            credits_text = values[header_map["credits"]].strip()
            prerequisites_text = values[
                header_map["prerequisites"]
            ].strip()
            cross_listed_text = values[
                header_map["crosslisted"]
            ].strip()

            if not course_code:
                continue

            normalized_code = normalize_course_code(course_code)

            parsed_catalog[normalized_code] = {
                "course_code": course_code,
                "title": title,
                "credits": parse_credits(credits_text),
                "prerequisites": extract_course_codes(
                    prerequisites_text
                ),
                "cross_listed": extract_course_codes(
                    cross_listed_text
                ),
            }

    return parsed_catalog


@app.get("/")
def health_check() -> dict[str, str]:
    """Basic endpoint used to verify that the API is running."""
    return {"status": "online"}


@app.post(
    "/api/v1/admin/catalog/import",
    status_code=status.HTTP_201_CREATED,
)
async def import_catalog(
    file: UploadFile = File(...),
) -> dict[str, Any]:
    """
    Accept an HTML catalog upload, parse its course table,
    and replace the current in-memory catalog.
    """
    filename = file.filename or ""

    if filename and not filename.lower().endswith(
        (".html", ".htm")
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file must be an HTML file",
        )

    raw_content = await file.read()

    if not raw_content:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file is empty",
        )

    html = raw_content.decode("utf-8-sig", errors="ignore")
    parsed_catalog = parse_catalog_html(html)

    if not parsed_catalog:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No valid course catalog table was found",
        )

    # Entirely replace the previous catalog.
    app.state.catalog = parsed_catalog

    return {
        "status": "success",
        "courses_imported": len(parsed_catalog),
    }


@app.get("/api/v1/catalog/courses/{course_code}")
def get_course(course_code: str) -> dict[str, Any]:
    """
    Retrieve one course using format-insensitive course-code matching.
    """
    normalized_code = normalize_course_code(course_code)
    course = app.state.catalog.get(normalized_code)

    if course is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Course not found",
        )

    return course