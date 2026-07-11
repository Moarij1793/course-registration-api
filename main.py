from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup
from fastapi import FastAPI, File, HTTPException, UploadFile, status
from pydantic import BaseModel


app = FastAPI(
    title="COSC 3506 Course Registration API",
    version="2.0.0",
)

# Phase 1: global catalog shared by all students.
app.state.catalog: dict[str, dict[str, Any]] = {}

# Phase 2: student-specific state.
#
# Example:
# {
#     "111": {
#         "history": [...],
#         "plan": [...]
#     }
# }
app.state.students: dict[str, dict[str, list[dict[str, Any]]]] = {}


# -------------------------------------------------------------------
# Pydantic request models
# -------------------------------------------------------------------


class HistoryCourse(BaseModel):
    course_code: str
    term: str
    credits_earned: int
    status: str


class HistoryPayload(BaseModel):
    history: list[HistoryCourse]


class PlannedCourse(BaseModel):
    course_code: str
    term: str


class PlanPayload(BaseModel):
    planned_courses: list[PlannedCourse]


# -------------------------------------------------------------------
# Shared helper functions
# -------------------------------------------------------------------


def normalize_course_code(course_code: str) -> str:
    """
    Make course-code comparison insensitive to spaces, hyphens,
    and letter case.

    Examples:
        COSC 3506 -> COSC3506
        COSC-3506 -> COSC3506
        cosc3506  -> COSC3506
    """
    return re.sub(r"[\s-]+", "", course_code).upper().strip()


def normalize_header(header: str) -> str:
    """
    Convert an HTML header to a simple comparison form.

    Example:
        Course Code -> coursecode
        Cross-listed -> crosslisted
    """
    return re.sub(r"[^a-z0-9]", "", header.lower())


def parse_credits(value: str) -> int:
    """
    Convert credits into an integer.

    Examples:
        3   -> 3
        3.0 -> 3
        blank or non-numeric -> 0
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
    Extract course codes from prerequisites or cross-listing text.

    Example:
        Requires COSC 2006 and ITEC-2007
        -> ["COSC 2006", "ITEC-2007"]
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


def get_student_or_404(student_id: str) -> dict[str, list[dict[str, Any]]]:
    """
    Return one student or raise HTTP 404.
    """
    student = app.state.students.get(student_id)

    if student is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Student not found",
        )

    return student


# -------------------------------------------------------------------
# Phase 1: catalog parser
# -------------------------------------------------------------------


def parse_catalog_html(html: str) -> dict[str, dict[str, Any]]:
    """
    Find the catalog table by its headers and parse all course rows.
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


# -------------------------------------------------------------------
# Phase 2: transcript parser
# -------------------------------------------------------------------


VALID_HISTORY_STATUSES = {
    "Completed",
    "In-Progress",
    "Attempted",
}


def grade_information_score(grade: str) -> int:
    """
    Rank transcript grades for duplicate-course resolution.

    Priority:
        numeric grade > letter grade > P or blank
    """
    cleaned_grade = grade.strip()

    # Numeric examples: 78, 78.5, 78%
    if re.fullmatch(r"\d+(?:\.\d+)?%?", cleaned_grade):
        return 3

    # Letter examples: A, A-, B+, C
    if re.fullmatch(
        r"[A-F](?:[+-])?",
        cleaned_grade,
        flags=re.IGNORECASE,
    ):
        return 2

    # P and blank are the least informative category.
    if cleaned_grade == "" or cleaned_grade.upper() == "P":
        return 1

    return 1


def parse_transcript_html(html: str) -> list[dict[str, Any]]:
    """
    Parse all relevant transcript tables.

    A valid history row must have:
        - Status: Completed, In-Progress, or Attempted
        - A non-empty term

    Duplicate key:
        (course_code, term)

    Duplicate winner:
        1. More informative grade
        2. Higher credits
    """
    soup = BeautifulSoup(html, "html.parser")

    deduplicated: dict[
        tuple[str, str],
        dict[str, Any],
    ] = {}

    required_headers = {
        "status",
        "course",
        "grade",
        "term",
        "credits",
    }

    for table in soup.find_all("table"):
        rows = table.find_all("tr")

        if not rows:
            continue

        header_map: dict[str, int] | None = None
        header_row_position: int | None = None

        # Search each table for a valid transcript header row.
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

            row_status = values[header_map["status"]].strip()
            course_code = values[header_map["course"]].strip()
            grade = values[header_map["grade"]].strip()
            term = values[header_map["term"]].strip()
            credits_text = values[header_map["credits"]].strip()

            # Canonical transcript inclusion rule.
            if row_status not in VALID_HISTORY_STATUSES:
                continue

            if not term:
                continue

            if not course_code:
                continue

            credits_earned = parse_credits(credits_text)
            grade_score = grade_information_score(grade)

            duplicate_key = (course_code, term)

            candidate = {
                "course_code": course_code,
                "term": term,
                "credits_earned": credits_earned,
                "status": row_status,
                "_grade_score": grade_score,
            }

            existing = deduplicated.get(duplicate_key)

            if existing is None:
                deduplicated[duplicate_key] = candidate
                continue

            existing_grade_score = existing["_grade_score"]
            existing_credits = existing["credits_earned"]

            candidate_is_better = (
                grade_score > existing_grade_score
                or (
                    grade_score == existing_grade_score
                    and credits_earned > existing_credits
                )
            )

            if candidate_is_better:
                deduplicated[duplicate_key] = candidate

    final_history: list[dict[str, Any]] = []

    for record in deduplicated.values():
        final_history.append(
            {
                "course_code": record["course_code"],
                "term": record["term"],
                "credits_earned": record["credits_earned"],
                "status": record["status"],
            }
        )

    return final_history


# -------------------------------------------------------------------
# General API endpoint
# -------------------------------------------------------------------


@app.get("/")
def health_check() -> dict[str, str]:
    return {"status": "online"}


# -------------------------------------------------------------------
# Phase 1 endpoints
# -------------------------------------------------------------------


@app.post(
    "/api/v1/admin/catalog/import",
    status_code=status.HTTP_201_CREATED,
)
async def import_catalog(
    file: UploadFile = File(...),
) -> dict[str, Any]:
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

    # Replace the previous global catalog.
    app.state.catalog = parsed_catalog

    return {
        "status": "success",
        "courses_imported": len(parsed_catalog),
    }


@app.get("/api/v1/catalog/courses/{course_code}")
def get_course(course_code: str) -> dict[str, Any]:
    normalized_code = normalize_course_code(course_code)
    course = app.state.catalog.get(normalized_code)

    if course is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Course not found",
        )

    return course


# -------------------------------------------------------------------
# Phase 2 history endpoints
# -------------------------------------------------------------------


@app.post(
    "/api/v1/students/{student_id}/history/import",
    status_code=status.HTTP_201_CREATED,
)
async def import_student_history(
    student_id: str,
    file: UploadFile = File(...),
) -> dict[str, Any]:
    raw_content = await file.read()

    if not raw_content:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file is empty",
        )

    html = raw_content.decode("utf-8-sig", errors="ignore")
    parsed_history = parse_transcript_html(html)

    # Preserve the student's existing plan on re-import.
    existing_student = app.state.students.get(student_id)

    if existing_student is None:
        existing_plan: list[dict[str, Any]] = []
    else:
        existing_plan = existing_student["plan"]

    app.state.students[student_id] = {
        "history": parsed_history,
        "plan": existing_plan,
    }

    return {
        "status": "success",
        "past_courses_imported": len(parsed_history),
    }


@app.put("/api/v1/students/{student_id}/history")
def replace_student_history(
    student_id: str,
    payload: HistoryPayload,
) -> dict[str, str]:
    student = get_student_or_404(student_id)

    student["history"] = [
        course.model_dump()
        for course in payload.history
    ]

    return {
        "status": "success",
        "message": "Academic history updated successfully",
    }


@app.delete("/api/v1/students/{student_id}/history")
def delete_student_history(
    student_id: str,
) -> dict[str, str]:
    student = get_student_or_404(student_id)
    student["history"] = []

    return {
        "status": "success",
        "message": "Academic history cleared successfully",
    }


# -------------------------------------------------------------------
# Phase 2 plan endpoints
# -------------------------------------------------------------------


@app.post("/api/v1/students/{student_id}/plan")
def save_student_plan(
    student_id: str,
    payload: PlanPayload,
) -> dict[str, Any]:
    student = get_student_or_404(student_id)

    student["plan"] = [
        course.model_dump()
        for course in payload.planned_courses
    ]

    return {
        "status": "success",
        "planned_courses_saved": len(student["plan"]),
    }


@app.put("/api/v1/students/{student_id}/plan")
def replace_student_plan(
    student_id: str,
    payload: PlanPayload,
) -> dict[str, Any]:
    student = get_student_or_404(student_id)

    student["plan"] = [
        course.model_dump()
        for course in payload.planned_courses
    ]

    return {
        "status": "success",
        "planned_courses_saved": len(student["plan"]),
    }


@app.delete("/api/v1/students/{student_id}/plan")
def delete_student_plan(
    student_id: str,
) -> dict[str, str]:
    student = get_student_or_404(student_id)
    student["plan"] = []

    return {
        "status": "success",
        "message": "Academic plan cleared successfully",
    }


# -------------------------------------------------------------------
# Phase 2 unified profile endpoint
# -------------------------------------------------------------------


@app.get("/api/v1/students/{student_id}/profile")
def get_student_profile(
    student_id: str,
) -> dict[str, Any]:
    student = get_student_or_404(student_id)

    # Return exactly these three top-level keys.
    return {
        "student_id": student_id,
        "history": student["history"],
        "plan": student["plan"],
    }


