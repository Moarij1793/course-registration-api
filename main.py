from __future__ import annotations

import bcrypt
import jwt
import time
import re

from collections import defaultdict
from typing import Any

from bs4 import BeautifulSoup
from fastapi import (
    Depends,
    FastAPI,
    File,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from fastapi.security import (
    HTTPAuthorizationCredentials,
    HTTPBearer,
)
from pydantic import BaseModel


app = FastAPI(
    title="COSC 3506 Course Registration API",
    version="4.0.0",
)


# ===================================================================
# APPLICATION STATE
# ===================================================================

app.state.catalog: dict[str, dict[str, Any]] = {}

app.state.students: dict[
    str,
    dict[str, list[dict[str, Any]]]
] = {}


# ===================================================================
# PHASE 4 AUTHENTICATION STATE
# ===================================================================

users_db: dict[str, dict[str, Any]] = {}

SECRET_KEY = "phase4secret"
ALGORITHM = "HS256"

security = HTTPBearer(auto_error=False)


# ===================================================================
# PYDANTIC MODELS
# ===================================================================


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


class RegisterRequest(BaseModel):
    username: str
    password: str


class LoginRequest(BaseModel):
    username: str
    password: str


# ===================================================================
# SHARED UTILITY FUNCTIONS
# ===================================================================


def normalize_course_code(course_code: str) -> str:
    """
    Convert differently formatted course codes into one comparison form.

    COSC 3506 -> COSC3506
    COSC-3506 -> COSC3506
    cosc3506  -> COSC3506
    """

    return re.sub(
        r"[\s-]+",
        "",
        course_code,
    ).upper().strip()


def normalize_header(header: str) -> str:
    """
    Normalize table headers.

    Course Code  -> coursecode
    Cross-listed -> crosslisted
    """

    return re.sub(
        r"[^a-z0-9]",
        "",
        header.lower(),
    )


def parse_credits(value: str) -> int:
    """
    Convert a credit string to an integer.

    Blank or non-numeric values become zero.
    """

    match = re.search(
        r"-?\d+(?:\.\d+)?",
        value,
    )

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

    pattern = (
        r"\b[A-Za-z]{2,10}\s*[- ]?\s*"
        r"\d{3,4}[A-Za-z]?\b"
    )

    matches = re.findall(
        pattern,
        cleaned_text,
    )

    extracted: list[str] = []
    seen: set[str] = set()

    for match in matches:
        display_code = re.sub(
            r"\s+",
            " ",
            match.strip(),
        )

        normalized_code = normalize_course_code(
            display_code
        )

        if normalized_code not in seen:
            seen.add(normalized_code)
            extracted.append(display_code)

    return extracted


def model_to_dict(
    model: BaseModel,
) -> dict[str, Any]:
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


# ===================================================================
# PHASE 1 CATALOG PARSER
# ===================================================================


def parse_catalog_html(
    html: str,
) -> dict[str, dict[str, Any]]:
    """
    Locate catalog tables by their headers and parse course rows.
    """

    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    parsed_catalog: dict[
        str,
        dict[str, Any],
    ] = {}

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

            cells = row.find_all(
                ["th", "td"]
            )

            headers = [
                normalize_header(
                    cell.get_text(
                        " ",
                        strip=True,
                    )
                )
                for cell in cells
            ]

            current_map = {
                header: index
                for index, header in enumerate(headers)
                if header
            }

            if required_headers.issubset(
                current_map.keys()
            ):
                header_map = current_map
                header_row_position = row_position
                break

        if (
            header_map is None
            or header_row_position is None
        ):
            continue

        for row in rows[
            header_row_position + 1:
        ]:

            cells = row.find_all(
                ["td", "th"]
            )

            if not cells:
                continue

            values = [
                cell.get_text(
                    " ",
                    strip=True,
                )
                for cell in cells
            ]

            highest_index = max(
                header_map.values()
            )

            if len(values) <= highest_index:
                continue

            course_code = values[
                header_map["coursecode"]
            ].strip()

            title = values[
                header_map["title"]
            ].strip()

            credits_text = values[
                header_map["credits"]
            ].strip()

            prerequisites_text = values[
                header_map["prerequisites"]
            ].strip()

            cross_listed_text = values[
                header_map["crosslisted"]
            ].strip()

            if not course_code:
                continue

            normalized_code = normalize_course_code(
                course_code
            )

            parsed_catalog[normalized_code] = {
                "course_code": course_code,
                "title": title,
                "credits": parse_credits(
                    credits_text
                ),
                "prerequisites": extract_course_codes(
                    prerequisites_text
                ),
                "cross_listed": extract_course_codes(
                    cross_listed_text
                ),
            }

    return parsed_catalog


# ===================================================================
# PHASE 2 TRANSCRIPT PARSER
# ===================================================================


VALID_HISTORY_STATUSES = {
    "Completed",
    "In-Progress",
    "Attempted",
}


def grade_information_score(
    grade: str,
) -> int:
    """
    Duplicate-selection priority:

    numeric grade > letter grade > P/blank
    """

    cleaned_grade = grade.strip()

    if re.fullmatch(
        r"\d+(?:\.\d+)?%?",
        cleaned_grade,
    ):
        return 3

    if re.fullmatch(
        r"[A-F](?:[+-])?",
        cleaned_grade,
        flags=re.IGNORECASE,
    ):
        return 2

    return 1


def parse_transcript_html(
    html: str,
) -> list[dict[str, Any]]:

    soup = BeautifulSoup(
        html,
        "html.parser",
    )

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

            cells = row.find_all(
                ["th", "td"]
            )

            headers = [
                normalize_header(
                    cell.get_text(
                        " ",
                        strip=True,
                    )
                )
                for cell in cells
            ]

            current_map = {
                header: index
                for index, header in enumerate(headers)
                if header
            }

            if required_headers.issubset(
                current_map.keys()
            ):
                header_map = current_map
                header_row_position = row_position
                break

        if (
            header_map is None
            or header_row_position is None
        ):
            continue

        for row in rows[
            header_row_position + 1:
        ]:

            cells = row.find_all(
                ["td", "th"]
            )

            if not cells:
                continue

            values = [
                cell.get_text(
                    " ",
                    strip=True,
                )
                for cell in cells
            ]

            highest_index = max(
                header_map.values()
            )

            if len(values) <= highest_index:
                continue

            row_status = values[
                header_map["status"]
            ].strip()

            course_code = values[
                header_map["course"]
            ].strip()

            grade = values[
                header_map["grade"]
            ].strip()

            term = values[
                header_map["term"]
            ].strip()

            credits_text = values[
                header_map["credits"]
            ].strip()

            if row_status not in VALID_HISTORY_STATUSES:
                continue

            if not term:
                continue

            if not course_code:
                continue

            credits_earned = parse_credits(
                credits_text
            )

            grade_score = grade_information_score(
                grade
            )

            duplicate_key = (
                course_code,
                term,
            )

            candidate = {
                "course_code": course_code,
                "term": term,
                "credits_earned": credits_earned,
                "status": row_status,
                "_grade_score": grade_score,
            }

            existing = deduplicated.get(
                duplicate_key
            )

            if existing is None:
                deduplicated[
                    duplicate_key
                ] = candidate

                continue

            existing_score = existing[
                "_grade_score"
            ]

            existing_credits = existing[
                "credits_earned"
            ]

            candidate_is_better = (
                grade_score > existing_score
                or (
                    grade_score == existing_score
                    and credits_earned
                    > existing_credits
                )
            )

            if candidate_is_better:
                deduplicated[
                    duplicate_key
                ] = candidate

    final_history: list[
        dict[str, Any]
    ] = []

    for record in deduplicated.values():

        final_history.append(
            {
                "course_code": record[
                    "course_code"
                ],
                "term": record[
                    "term"
                ],
                "credits_earned": record[
                    "credits_earned"
                ],
                "status": record[
                    "status"
                ],
            }
        )

    return final_history


# ===================================================================
# PHASE 3 TERM / AUDIT UTILITIES
# ===================================================================


SEASON_ORDER = {
    "W": 1,
    "SP": 2,
    "S": 3,
    "F": 4,
}


def parse_term(
    term: str,
) -> tuple[int, int]:

    cleaned_term = term.strip().upper()

    match = re.fullmatch(
        r"(\d{2})(W|SP|S|F)",
        cleaned_term,
    )

    if not match:
        return (999, 999)

    year = int(match.group(1))
    season = match.group(2)

    return (
        year,
        SEASON_ORDER[season],
    )


def is_strictly_earlier(
    completed_term: str,
    planned_term: str,
) -> bool:

    return (
        parse_term(completed_term)
        < parse_term(planned_term)
    )


def get_completed_history(
    history: list[dict[str, Any]],
) -> list[dict[str, Any]]:

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

    normalized_prerequisite = normalize_course_code(
        prerequisite_code
    )

    for record in history:

        if record.get("status") != "Completed":
            continue

        history_code = normalize_course_code(
            str(
                record.get(
                    "course_code",
                    "",
                )
            )
        )

        if (
            history_code
            != normalized_prerequisite
        ):
            continue

        history_term = str(
            record.get(
                "term",
                "",
            )
        )

        if is_strictly_earlier(
            history_term,
            planned_term,
        ):
            return True

    return False


def build_timeline_validation(
    history: list[dict[str, Any]],
    plan: list[dict[str, Any]],
) -> list[dict[str, Any]]:

    errors_by_term: dict[
        str,
        list[dict[str, str]],
    ] = defaultdict(list)

    for planned_course in plan:

        course_code = str(
            planned_course.get(
                "course_code",
                "",
            )
        )

        planned_term = str(
            planned_course.get(
                "term",
                "",
            )
        )

        catalog_course = app.state.catalog.get(
            normalize_course_code(course_code)
        )

        if catalog_course is None:
            continue

        prerequisites = catalog_course.get(
            "prerequisites",
            [],
        )

        for prerequisite in prerequisites:

            if prerequisite_is_satisfied(
                prerequisite,
                planned_term,
                history,
            ):
                continue

            errors_by_term[
                planned_term
            ].append(
                {
                    "course_code": course_code,
                    "type": "MISSING_PREREQUISITE",
                    "message": (
                        "Missing prerequisite: "
                        f"{prerequisite}"
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

    for record in completed_history:

        display_code = str(
            record.get(
                "course_code",
                "",
            )
        )

        if (
            normalize_course_code(
                display_code
            )
            == normalized_code
        ):
            return display_code

    return None


def build_cross_list_violations(
    history: list[dict[str, Any]],
    plan: list[dict[str, Any]],
) -> list[dict[str, str]]:

    completed_history = get_completed_history(
        history
    )

    completed_codes = {
        normalize_course_code(
            str(
                record.get(
                    "course_code",
                    "",
                )
            )
        )
        for record in completed_history
    }

    violations: list[
        dict[str, str]
    ] = []

    seen_violations: set[
        tuple[str, str]
    ] = set()

    for planned_course in plan:

        planned_code = str(
            planned_course.get(
                "course_code",
                "",
            )
        )

        planned_normalized = normalize_course_code(
            planned_code
        )

        catalog_course = app.state.catalog.get(
            planned_normalized
        )

        if catalog_course is None:
            continue

        direct_cross_listings = {
            normalize_course_code(code)
            for code in catalog_course.get(
                "cross_listed",
                [],
            )
        }

        reverse_cross_listings: set[str] = set()

        for completed_code in completed_codes:

            completed_catalog_course = (
                app.state.catalog.get(
                    completed_code
                )
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

            if (
                planned_normalized
                in completed_cross_listed
            ):
                reverse_cross_listings.add(
                    completed_code
                )

        conflicting_completed_codes = (
            direct_cross_listings.intersection(
                completed_codes
            )
            | reverse_cross_listings
        )

        for completed_code in sorted(
            conflicting_completed_codes
        ):

            completed_display_code = (
                find_completed_display_code(
                    completed_code,
                    completed_history,
                )
            )

            if completed_display_code is None:
                completed_display_code = (
                    completed_code
                )

            violation_key = (
                planned_normalized,
                completed_code,
            )

            if violation_key in seen_violations:
                continue

            seen_violations.add(
                violation_key
            )

            violations.append(
                {
                    "course_code": planned_code,
                    "type": "CROSS_LIST_CONFLICT",
                    "message": (
                        "Cross-listed with completed "
                        f"course {completed_display_code}"
                    ),
                }
            )

    return violations


def calculate_total_earned(
    history: list[dict[str, Any]],
) -> int:

    completed_credits: dict[
        str,
        int,
    ] = {}

    for record in history:

        if record.get("status") != "Completed":
            continue

        normalized_code = normalize_course_code(
            str(
                record.get(
                    "course_code",
                    "",
                )
            )
        )

        credits = int(
            record.get(
                "credits_earned",
                0,
            )
            or 0
        )

        if normalized_code not in completed_credits:

            completed_credits[
                normalized_code
            ] = credits

        else:

            completed_credits[
                normalized_code
            ] = max(
                completed_credits[
                    normalized_code
                ],
                credits,
            )

    return sum(
        completed_credits.values()
    )


def calculate_total_planned(
    plan: list[dict[str, Any]],
) -> int:

    total = 0

    for planned_course in plan:

        course_code = str(
            planned_course.get(
                "course_code",
                "",
            )
        )

        catalog_course = app.state.catalog.get(
            normalize_course_code(course_code)
        )

        if catalog_course is None:
            continue

        total += int(
            catalog_course.get(
                "credits",
                0,
            )
            or 0
        )

    return total


def build_credit_summary(
    history: list[dict[str, Any]],
    plan: list[dict[str, Any]],
) -> dict[str, int]:

    total_earned = calculate_total_earned(
        history
    )

    total_planned = calculate_total_planned(
        plan
    )

    total_remaining = max(
        0,
        120
        - total_earned
        - total_planned,
    )

    return {
        "total_earned": total_earned,
        "total_planned": total_planned,
        "total_remaining_for_graduation": total_remaining,
    }


# ===================================================================
# PHASE 4 AUTHENTICATION
# ===================================================================


def create_access_token(
    username: str,
    role: str,
) -> str:

    return jwt.encode(
        {
            "sub": username,
            "role": role,
        },
        SECRET_KEY,
        algorithm=ALGORITHM,
    )


def verify_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(
        security
    ),
) -> dict[str, Any]:

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
        )

    try:

        payload = jwt.decode(
            credentials.credentials,
            SECRET_KEY,
            algorithms=[ALGORITHM],
        )

        username = payload.get("sub")

        if not username:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized",
            )

        return payload

    except jwt.PyJWTError:

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
        )


def require_owner(
    student_id: str,
    token: dict[str, Any],
) -> None:
    """
    Strict BOLA protection.

    Used for history import.

    The JWT sub MUST exactly equal student_id.
    Admin does not bypass this rule.
    """

    username = token.get("sub")

    if username != student_id:

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
        )


def require_owner_or_admin(
    student_id: str,
    token: dict[str, Any],
) -> None:
    """
    RBAC protection.

    Student:
        Can access own student ID.

    Admin:
        Can access any student ID.
    """

    username = token.get("sub")
    role = token.get("role")

    if role == "admin":
        return

    if username != student_id:

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
        )


# ===================================================================
# PHASE 4 AUTH ENDPOINTS
# ===================================================================


@app.post(
    "/api/v1/auth/register",
    status_code=status.HTTP_201_CREATED,
)
def register(
    data: RegisterRequest,
) -> dict[str, str]:

    username = data.username
    password = data.password

    if username in users_db:

        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User exists",
        )

    hashed = bcrypt.hashpw(
        password.encode("utf-8"),
        bcrypt.gensalt(),
    )

    users_db[username] = {
        "password": hashed,
        "role": "student",
    }

    return {
        "status": "registered",
    }


@app.post("/api/v1/auth/login")
def login(
    data: LoginRequest,
) -> dict[str, str]:

    username = data.username
    password = data.password

    # Hardcoded admin account
    if (
        username == "admin"
        and password == "admin"
    ):

        token = create_access_token(
            username="admin",
            role="admin",
        )

        return {
            "access_token": token,
            "token_type": "bearer",
        }

    user = users_db.get(username)

    if user is None:

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
        )

    stored_password = user["password"]

    if not bcrypt.checkpw(
        password.encode("utf-8"),
        stored_password,
    ):

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
        )

    token = create_access_token(
        username=username,
        role=user["role"],
    )

    return {
        "access_token": token,
        "token_type": "bearer",
    }


# ===================================================================
# HEALTH ENDPOINT
# ===================================================================


@app.get("/")
def health_check() -> dict[str, str]:

    return {
        "status": "online"
    }


# ===================================================================
# PHASE 1 CATALOG ENDPOINTS
# ===================================================================


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

    html = raw_content.decode(
        "utf-8-sig",
        errors="ignore",
    )

    parsed_catalog = parse_catalog_html(
        html
    )

    if not parsed_catalog:

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No valid course catalog table was found",
        )

    app.state.catalog = parsed_catalog

    return {
        "status": "success",
        "courses_imported": len(
            parsed_catalog
        ),
    }


@app.get(
    "/api/v1/catalog/courses/{course_code}"
)
def get_course(
    course_code: str,
) -> dict[str, Any]:

    normalized_code = normalize_course_code(
        course_code
    )

    course = app.state.catalog.get(
        normalized_code
    )

    if course is None:

        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Course not found",
        )

    return course


# ===================================================================
# PHASE 2 HISTORY ENDPOINTS
# ===================================================================


@app.post(
    "/api/v1/students/{student_id}/history/import",
    status_code=status.HTTP_201_CREATED,
)
async def import_student_history(
    student_id: str,
    file: UploadFile = File(...),
    token: dict[str, Any] = Depends(
        verify_token
    ),
) -> dict[str, Any]:

    # Strict BOLA:
    # token sub MUST equal student_id.
    require_owner(
        student_id,
        token,
    )

    raw_content = await file.read()

    if not raw_content:

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file is empty",
        )

    html = raw_content.decode(
        "utf-8-sig",
        errors="ignore",
    )

    parsed_history = parse_transcript_html(
        html
    )

    existing_student = app.state.students.get(
        student_id
    )

    if existing_student is None:
        existing_plan: list[
            dict[str, Any]
        ] = []
    else:
        existing_plan = existing_student[
            "plan"
        ]

    app.state.students[student_id] = {
        "history": parsed_history,
        "plan": existing_plan,
    }

    return {
        "status": "success",
        "past_courses_imported": len(
            parsed_history
        ),
    }


@app.put(
    "/api/v1/students/{student_id}/history"
)
def replace_student_history(
    student_id: str,
    payload: HistoryPayload,
) -> dict[str, str]:

    student = get_student_or_404(
        student_id
    )

    student["history"] = [
        model_to_dict(course)
        for course in payload.history
    ]

    return {
        "status": "success",
        "message": (
            "Academic history updated successfully"
        ),
    }


@app.delete(
    "/api/v1/students/{student_id}/history"
)
def delete_student_history(
    student_id: str,
) -> dict[str, str]:

    student = get_student_or_404(
        student_id
    )

    student["history"] = []

    return {
        "status": "success",
        "message": (
            "Academic history cleared successfully"
        ),
    }


# ===================================================================
# PHASE 2 PLAN ENDPOINTS
# ===================================================================


@app.post(
    "/api/v1/students/{student_id}/plan"
)
def save_student_plan(
    student_id: str,
    payload: PlanPayload,
) -> dict[str, Any]:

    student = get_student_or_404(
        student_id
    )

    student["plan"] = [
        model_to_dict(course)
        for course in payload.planned_courses
    ]

    return {
        "status": "success",
        "planned_courses_saved": len(
            student["plan"]
        ),
    }


@app.put(
    "/api/v1/students/{student_id}/plan"
)
def replace_student_plan(
    student_id: str,
    payload: PlanPayload,
) -> dict[str, Any]:

    student = get_student_or_404(
        student_id
    )

    student["plan"] = [
        model_to_dict(course)
        for course in payload.planned_courses
    ]

    return {
        "status": "success",
        "planned_courses_saved": len(
            student["plan"]
        ),
    }


@app.get(
    "/api/v1/students/{student_id}/plan"
)
def get_student_plan(
    student_id: str,
    token: dict[str, Any] = Depends(
        verify_token
    ),
) -> dict[str, Any]:

    require_owner_or_admin(
        student_id,
        token,
    )

    student = get_student_or_404(
        student_id
    )

    return {
        "student_id": student_id,
        "planned_courses": student[
            "plan"
        ],
    }


@app.delete(
    "/api/v1/students/{student_id}/plan"
)
def delete_student_plan(
    student_id: str,
) -> dict[str, str]:

    student = get_student_or_404(
        student_id
    )

    student["plan"] = []

    return {
        "status": "success",
        "message": (
            "Academic plan cleared successfully"
        ),
    }


# ===================================================================
# PHASE 2 PROFILE ENDPOINT
# ===================================================================


@app.get(
    "/api/v1/students/{student_id}/profile"
)
def get_student_profile(
    student_id: str,
    token: dict[str, Any] = Depends(
        verify_token
    ),
) -> dict[str, Any]:

    require_owner_or_admin(
        student_id,
        token,
    )

    student = get_student_or_404(
        student_id
    )

    return {
        "student_id": student_id,
        "history": student["history"],
        "plan": student["plan"],
    }


# ===================================================================
# PHASE 4 RATE LIMITER
# ===================================================================


audit_requests: dict[
    str,
    list[float],
] = defaultdict(list)

RATE_LIMIT = 10
RATE_WINDOW = 60


def check_audit_rate_limit(
    identity: str,
) -> None:

    now = time.time()

    request_times = audit_requests[
        identity
    ]

    # Remove requests older than 60 seconds.
    request_times[:] = [
        request_time
        for request_time in request_times
        if now - request_time
        < RATE_WINDOW
    ]

    if len(request_times) >= RATE_LIMIT:

        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded",
        )

    request_times.append(now)


# ===================================================================
# PHASE 3 + PHASE 4 AUDIT ENDPOINT
# ===================================================================


@app.get(
    "/api/v1/students/{student_id}/audit-report"
)
def get_audit_report(
    student_id: str,
    strict: bool = Query(
        default=False
    ),
    token: dict[str, Any] = Depends(
        verify_token
    ),
) -> dict[str, Any]:

    require_owner_or_admin(
        student_id,
        token,
    )

    identity = str(
        token.get("sub")
    )

    check_audit_rate_limit(
        identity
    )

    student = get_student_or_404(
        student_id
    )

    history = student["history"]
    plan = student["plan"]

    timeline_validation = (
        build_timeline_validation(
            history,
            plan,
        )
    )

    cross_list_violations = (
        build_cross_list_violations(
            history,
            plan,
        )
    )

    credit_summary = (
        build_credit_summary(
            history,
            plan,
        )
    )

    has_issues = bool(
        timeline_validation
        or cross_list_violations
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
        "timeline_validation": (
            timeline_validation
        ),
        "cross_list_violations": (
            cross_list_violations
        ),
        "credit_summary": credit_summary,
    }


# ===================================================================
# PHASE 4 RECOMMENDATION ENGINE
# ===================================================================


def get_next_term(
    term: str,
) -> str:
    """
    Return the next academic term.

    Example:

    26W -> 26SP
    26SP -> 26S
    26S -> 26F
    26F -> 27W
    """

    year, season_number = parse_term(
        term
    )

    if season_number == 999:
        return "26F"

    season_codes = [
        "W",
        "SP",
        "S",
        "F",
    ]

    current_index = season_number - 1
    next_index = current_index + 1
    next_year = year

    if next_index >= len(
        season_codes
    ):
        next_index = 0
        next_year += 1

    return (
        f"{next_year:02d}"
        f"{season_codes[next_index]}"
    )


def build_recommendation_pathway(
    history: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Compute a graduation pathway using
    Kahn's topological sorting algorithm.

    Completed courses are excluded.

    Courses whose prerequisites are completed
    are available in the first term.

    Remaining prerequisite relationships are
    represented as a DAG.
    """

    # ---------------------------------------------------------------
    # 1. Identify completed courses
    # ---------------------------------------------------------------

    completed_courses: set[str] = set()

    for record in history:

        if record.get("status") != "Completed":
            continue

        completed_courses.add(
            normalize_course_code(
                str(
                    record.get(
                        "course_code",
                        "",
                    )
                )
            )
        )

    # ---------------------------------------------------------------
    # 2. Find all courses not yet completed
    # ---------------------------------------------------------------

    remaining_courses: dict[
        str,
        dict[str, Any],
    ] = {}

    for code, course in app.state.catalog.items():

        if code in completed_courses:
            continue

        remaining_courses[
            code
        ] = course

    # ---------------------------------------------------------------
    # 3. Build graph
    #
    # If:
    #
    # A -> B
    #
    # then B depends on A.
    # ---------------------------------------------------------------

    graph: dict[
        str,
        set[str],
    ] = {
        code: set()
        for code in remaining_courses
    }

    indegree: dict[
        str,
        int,
    ] = {
        code: 0
        for code in remaining_courses
    }

    for course_code, course in (
        remaining_courses.items()
    ):

        prerequisites = course.get(
            "prerequisites",
            [],
        )

        for prerequisite in prerequisites:

            prerequisite_code = (
                normalize_course_code(
                    str(prerequisite)
                )
            )

            # Already completed.
            if (
                prerequisite_code
                in completed_courses
            ):
                continue

            # Prerequisite isn't present in
            # the catalog.
            if (
                prerequisite_code
                not in remaining_courses
            ):
                continue

            if (
                course_code
                not in graph[
                    prerequisite_code
                ]
            ):

                graph[
                    prerequisite_code
                ].add(course_code)

                indegree[
                    course_code
                ] += 1

    # ---------------------------------------------------------------
    # 4. Kahn's algorithm
    # ---------------------------------------------------------------

    available = sorted(
        [
            code
            for code, degree
            in indegree.items()
            if degree == 0
        ]
    )

    pathway_levels: list[
        list[str]
    ] = []

    while available:

        current_level = sorted(
            available
        )

        pathway_levels.append(
            current_level
        )

        next_available: list[
            str
        ] = []

        for course_code in current_level:

            for dependent in graph[
                course_code
            ]:

                indegree[
                    dependent
                ] -= 1

                if (
                    indegree[
                        dependent
                    ]
                    == 0
                ):

                    next_available.append(
                        dependent
                    )

        available = sorted(
            next_available
        )

    # ---------------------------------------------------------------
    # 5. Detect cycles
    # ---------------------------------------------------------------

    scheduled_count = sum(
        len(level)
        for level in pathway_levels
    )

    if scheduled_count != len(
        remaining_courses
    ):

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Course prerequisite graph "
                "contains a cycle"
            ),
        )

    # ---------------------------------------------------------------
    # 6. Determine first recommended term
    # ---------------------------------------------------------------

    completed_terms = [
        str(
            record.get(
                "term",
                "",
            )
        )
        for record in history
        if record.get(
            "status"
        ) == "Completed"
    ]

    valid_completed_terms = [
        term
        for term in completed_terms
        if parse_term(term)
        != (999, 999)
    ]

    if valid_completed_terms:

        latest_completed_term = max(
            valid_completed_terms,
            key=parse_term,
        )

        starting_term = get_next_term(
            latest_completed_term
        )

    else:

        starting_term = "26F"

    # ---------------------------------------------------------------
    # 7. Convert graph levels into chronological terms
    # ---------------------------------------------------------------

    pathway: list[
        dict[str, Any]
    ] = []

    current_term = starting_term

    for level in pathway_levels:

        pathway.append(
            {
                "term": current_term,
                "courses": sorted(level),
            }
        )

        current_term = get_next_term(
            current_term
        )

    return pathway


# ===================================================================
# PHASE 4 RECOMMENDATIONS ENDPOINT
# ===================================================================


@app.get(
    "/api/v1/students/{student_id}/recommendations"
)
def get_recommendations(
    student_id: str,
    token: dict[str, Any] = Depends(
        verify_token
    ),
) -> dict[str, Any]:

    require_owner_or_admin(
        student_id,
        token,
    )

    student = get_student_or_404(
        student_id
    )

    pathway = (
        build_recommendation_pathway(
            student["history"]
        )
    )

    return {
        "student_id": student_id,
        "recommended_pathway": pathway,
    }


