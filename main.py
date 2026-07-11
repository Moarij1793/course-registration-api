from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from bs4 import BeautifulSoup
from fastapi import FastAPI, File, HTTPException, Query, UploadFile, status
from pydantic import BaseModel


app = FastAPI(
    title="COSC 3506 Course Registration API",
    version="3.0.0",
)

# -------------------------------------------------------------------
# Application state
# -------------------------------------------------------------------

# Phase 1 catalog:
# {
#     "COSC3506": {
#         "course_code": "COSC 3506",
#         "title": "Software Systems Development",
#         "credits": 3,
#         "prerequisites": ["COSC 2007"],
#         "cross_listed": ["ITEC 3506"],
#     }
# }
app.state.catalog: dict[str, dict[str, Any]] = {}

# Phase 2 students:
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
# Shared utility functions
# -------------------------------------------------------------------


def normalize_course_code(course_code: str) -> str:
    """
    Convert differently formatted course codes into one comparison form.

    COSC 3506 -> COSC3506
    COSC-3506 -> COSC3506
    cosc3506  -> COSC3506
    """
    return re.sub(r"[\s-]+", "", course_code).upper().strip()


def normalize_header(header: str) -> str:
    """
    Normalize table headers.

    Course Code  -> coursecode
    Cross-listed -> crosslisted
    """
    return re.sub(r"[^a-z0-9]", "", header.lower())


def parse_credits(value: str) -> int:
    """
    Convert a credit string to an integer.

    Blank or non-numeric values become zero.
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
    Extract course codes from prerequisite and cross-listing text.
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

    extracted: list[str] = []
    seen: set[str] = set()

    for match in matches:
        display_code = re.sub(r"\s+", " ", match.strip())
        normalized_code = normalize_course_code(display_code)

        if normalized_code not in seen:
            seen.add(normalized_code)
            extracted.append(display_code)

    return extracted


def model_to_dict(model: BaseModel) -> dict[str, Any]:
    """
    Support both Pydantic version 1 and version 2.
    """
    if hasattr(model, "model_dump"):
        return model.model_dump()

    return model.dict()


def get_student_or_404(
    student_id: str,
) -> dict[str, list[dict[str, Any]]]:
    student = app.state.students.get(student_id)

    if student is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Student not found",
        )

    return student


# -------------------------------------------------------------------
# Phase 1 catalog parser
# -------------------------------------------------------------------


def parse_catalog_html(html: str) -> dict[str, dict[str, Any]]:
    """
    Locate catalog tables by their headers and parse course rows.
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
            highest_index = max(header_map.values())

            if len(values) <= highest_index:
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
# Phase 2 transcript parser
# -------------------------------------------------------------------


VALID_HISTORY_STATUSES = {
    "Completed",
    "In-Progress",
    "Attempted",
}


def grade_information_score(grade: str) -> int:
    """
    Duplicate-selection priority:

    numeric grade > letter grade > P/blank
    """
    cleaned_grade = grade.strip()

    if re.fullmatch(r"\d+(?:\.\d+)?%?", cleaned_grade):
        return 3

    if re.fullmatch(
        r"[A-F](?:[+-])?",
        cleaned_grade,
        flags=re.IGNORECASE,
    ):
        return 2

    return 1


def parse_transcript_html(html: str) -> list[dict[str, Any]]:
    """
    Parse transcript tables using the canonical Phase 2 rules.
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
            highest_index = max(header_map.values())

            if len(values) <= highest_index:
                continue

            row_status = values[header_map["status"]].strip()
            course_code = values[header_map["course"]].strip()
            grade = values[header_map["grade"]].strip()
            term = values[header_map["term"]].strip()
            credits_text = values[header_map["credits"]].strip()

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

            existing_score = existing["_grade_score"]
            existing_credits = existing["credits_earned"]

            candidate_is_better = (
                grade_score > existing_score
                or (
                    grade_score == existing_score
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
# Phase 3 term and audit utilities
# -------------------------------------------------------------------


SEASON_ORDER = {
    "W": 1,
    "SP": 2,
    "S": 3,
    "F": 4,
}


def parse_term(term: str) -> tuple[int, int]:
    """
    Convert a term into a sortable tuple.

    23F  -> (23, 4)
    24W  -> (24, 1)
    26SP -> (26, 2)
    """
    cleaned_term = term.strip().upper()

    match = re.fullmatch(r"(\d{2})(W|SP|S|F)", cleaned_term)

    if not match:
        # Unknown terms are placed after valid terms instead of crashing.
        return (999, 999)

    year = int(match.group(1))
    season = match.group(2)

    return (year, SEASON_ORDER[season])


def is_strictly_earlier(
    completed_term: str,
    planned_term: str,
) -> bool:
    """
    Return True only when the completed term occurs before the
    planned term.
    """
    return parse_term(completed_term) < parse_term(planned_term)


def get_completed_history(
    history: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Return only completed history records.
    """
    return [
        record
        for record in history
        if record.get("status") == "Completed"
    ]


def prerequisite_is_satisfied(
    prerequisite_code: str,
    planned_term: str,
    history: list[dict[str, Any]],
) -> bool:
    """
    A prerequisite is satisfied only when it appears as Completed
    in a strictly earlier history term.
    """
    normalized_prerequisite = normalize_course_code(
        prerequisite_code
    )

    for record in history:
        if record.get("status") != "Completed":
            continue

        history_code = normalize_course_code(
            str(record.get("course_code", ""))
        )

        if history_code != normalized_prerequisite:
            continue

        history_term = str(record.get("term", ""))

        if is_strictly_earlier(history_term, planned_term):
            return True

    return False


def build_timeline_validation(
    history: list[dict[str, Any]],
    plan: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Build prerequisite errors grouped by planned term.
    """
    errors_by_term: dict[str, list[dict[str, str]]] = defaultdict(list)

    for planned_course in plan:
        course_code = str(
            planned_course.get("course_code", "")
        )
        planned_term = str(planned_course.get("term", ""))

        catalog_course = app.state.catalog.get(
            normalize_course_code(course_code)
        )

        # A course missing from the catalog cannot provide
        # prerequisite information. Do not invent an extra error type.
        if catalog_course is None:
            continue

        prerequisites = catalog_course.get("prerequisites", [])

        for prerequisite in prerequisites:
            if prerequisite_is_satisfied(
                prerequisite,
                planned_term,
                history,
            ):
                continue

            errors_by_term[planned_term].append(
                {
                    "course_code": course_code,
                    "type": "MISSING_PREREQUISITE",
                    "message": (
                        f"Missing prerequisite: {prerequisite}"
                    ),
                }
            )

    ordered_terms = sorted(
        errors_by_term.keys(),
        key=parse_term,
    )

    return [
        {
            "term": term,
            "errors": errors_by_term[term],
        }
        for term in ordered_terms
    ]


def find_completed_display_code(
    normalized_code: str,
    completed_history: list[dict[str, Any]],
) -> str | None:
    """
    Return the original transcript course code for a completed course.
    """
    for record in completed_history:
        display_code = str(record.get("course_code", ""))

        if normalize_course_code(display_code) == normalized_code:
            return display_code

    return None


def build_cross_list_violations(
    history: list[dict[str, Any]],
    plan: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """
    Detect when a planned course is cross-listed with a course that
    the student has already completed.
    """
    completed_history = get_completed_history(history)

    completed_codes = {
        normalize_course_code(
            str(record.get("course_code", ""))
        )
        for record in completed_history
    }

    violations: list[dict[str, str]] = []
    seen_violations: set[tuple[str, str]] = set()

    for planned_course in plan:
        planned_code = str(
            planned_course.get("course_code", "")
        )
        planned_normalized = normalize_course_code(planned_code)

        catalog_course = app.state.catalog.get(planned_normalized)

        if catalog_course is None:
            continue

        direct_cross_listings = {
            normalize_course_code(code)
            for code in catalog_course.get("cross_listed", [])
        }

        # Also inspect completed-course catalog records so the check
        # works if cross-listing is declared only in the other direction.
        reverse_cross_listings: set[str] = set()

        for completed_code in completed_codes:
            completed_catalog_course = app.state.catalog.get(
                completed_code
            )

            if completed_catalog_course is None:
                continue

            completed_cross_listed = {
                normalize_course_code(code)
                for code in completed_catalog_course.get(
                    "cross_listed",
                    [],
                )
            }

            if planned_normalized in completed_cross_listed:
                reverse_cross_listings.add(completed_code)

        conflicting_completed_codes = (
            direct_cross_listings.intersection(completed_codes)
            | reverse_cross_listings
        )

        for completed_code in sorted(conflicting_completed_codes):
            completed_display_code = find_completed_display_code(
                completed_code,
                completed_history,
            )

            if completed_display_code is None:
                completed_display_code = completed_code

            violation_key = (
                planned_normalized,
                completed_code,
            )

            if violation_key in seen_violations:
                continue

            seen_violations.add(violation_key)

            violations.append(
                {
                    "course_code": planned_code,
                    "type": "CROSS_LIST_CONFLICT",
                    "message": (
                        "Cross-listed with completed course "
                        f"{completed_display_code}"
                    ),
                }
            )

    return violations


def calculate_total_earned(
    history: list[dict[str, Any]],
) -> int:
    """
    Count completed course credits once per normalized course code.

    Attempted and In-Progress courses contribute zero.
    Multiple completed entries for the same course do not double-count.
    """
    completed_credits: dict[str, int] = {}

    for record in history:
        if record.get("status") != "Completed":
            continue

        normalized_code = normalize_course_code(
            str(record.get("course_code", ""))
        )

        credits = int(record.get("credits_earned", 0) or 0)

        if normalized_code not in completed_credits:
            completed_credits[normalized_code] = credits
        else:
            completed_credits[normalized_code] = max(
                completed_credits[normalized_code],
                credits,
            )

    return sum(completed_credits.values())


def calculate_total_planned(
    plan: list[dict[str, Any]],
) -> int:
    """
    Sum catalog credits for every planned course.
    """
    total = 0

    for planned_course in plan:
        course_code = str(
            planned_course.get("course_code", "")
        )

        catalog_course = app.state.catalog.get(
            normalize_course_code(course_code)
        )

        if catalog_course is None:
            continue

        total += int(catalog_course.get("credits", 0) or 0)

    return total


def build_credit_summary(
    history: list[dict[str, Any]],
    plan: list[dict[str, Any]],
) -> dict[str, int]:
    """
    Calculate earned, planned, and remaining graduation credits.
    """
    total_earned = calculate_total_earned(history)
    total_planned = calculate_total_planned(plan)

    total_remaining = max(
        0,
        120 - total_earned - total_planned,
    )

    return {
        "total_earned": total_earned,
        "total_planned": total_planned,
        "total_remaining_for_graduation": total_remaining,
    }


# -------------------------------------------------------------------
# Health endpoint
# -------------------------------------------------------------------


@app.get("/")
def health_check() -> dict[str, str]:
    return {"status": "online"}


# -------------------------------------------------------------------
# Phase 1 catalog endpoints
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
        model_to_dict(course)
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
        model_to_dict(course)
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
        model_to_dict(course)
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
# Phase 2 profile endpoint
# -------------------------------------------------------------------


@app.get("/api/v1/students/{student_id}/profile")
def get_student_profile(
    student_id: str,
) -> dict[str, Any]:
    student = get_student_or_404(student_id)

    return {
        "student_id": student_id,
        "history": student["history"],
        "plan": student["plan"],
    }


# -------------------------------------------------------------------
# Phase 3 audit endpoint
# -------------------------------------------------------------------


@app.get("/api/v1/students/{student_id}/audit-report")
def get_audit_report(
    student_id: str,
    strict: bool = Query(default=False),
) -> dict[str, Any]:
    student = get_student_or_404(student_id)

    history = student["history"]
    plan = student["plan"]

    timeline_validation = build_timeline_validation(
        history,
        plan,
    )

    cross_list_violations = build_cross_list_violations(
        history,
        plan,
    )

    credit_summary = build_credit_summary(
        history,
        plan,
    )

    has_issues = bool(
        timeline_validation or cross_list_violations
    )

    if not has_issues:
        audit_status = "ok"
    elif strict:
        audit_status = "failed"
    else:
        audit_status = "warning"

    return {
        "student_id": student_id,
        "status": audit_status,
        "timeline_validation": timeline_validation,
        "cross_list_violations": cross_list_violations,
        "credit_summary": credit_summary,
    }


    