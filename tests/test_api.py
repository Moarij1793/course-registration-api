import pytest
from fastapi.testclient import TestClient

from main import (
    app,
    extract_course_codes,
    normalize_course_code,
    parse_credits,
    parse_term,
)


client = TestClient(app)


CATALOG_HTML = """
<html>
<body>
<table>
    <tr>
        <th>Course Code</th>
        <th>Title</th>
        <th>Credits</th>
        <th>Prerequisites</th>
        <th>Cross-listed</th>
    </tr>
    <tr>
        <td>COSC 2006</td>
        <td>Programming Fundamentals I</td>
        <td>3</td>
        <td>None</td>
        <td></td>
    </tr>
    <tr>
        <td>COSC 2007</td>
        <td>Programming Fundamentals II</td>
        <td>3</td>
        <td>COSC 2006</td>
        <td></td>
    </tr>
    <tr>
        <td>COSC 3506</td>
        <td>Software Development</td>
        <td>3</td>
        <td>COSC 2007</td>
        <td>ITEC 3506</td>
    </tr>
    <tr>
        <td>ITEC 3506</td>
        <td>Software Development</td>
        <td>3</td>
        <td>COSC 2007</td>
        <td>COSC 3506</td>
    </tr>
</table>
</body>
</html>
"""


TRANSCRIPT_HTML = """
<html>
<body>
<table>
    <tr>
        <th>Status</th>
        <th>Course</th>
        <th>Title</th>
        <th>Grade</th>
        <th>Term</th>
        <th>Credits</th>
    </tr>
    <tr>
        <td>Completed</td>
        <td>COSC-2006</td>
        <td>Programming Fundamentals I</td>
        <td>B</td>
        <td>23F</td>
        <td>3</td>
    </tr>
    <tr>
        <td>Completed</td>
        <td>COSC-2006</td>
        <td>Programming Fundamentals I</td>
        <td>85</td>
        <td>23F</td>
        <td>3</td>
    </tr>
    <tr>
        <td>Completed</td>
        <td>COSC-2006</td>
        <td>Programming Fundamentals I</td>
        <td>A</td>
        <td>24W</td>
        <td>3</td>
    </tr>
    <tr>
        <td>Attempted</td>
        <td>COSC-2007</td>
        <td>Programming Fundamentals II</td>
        <td>F</td>
        <td>24F</td>
        <td>0</td>
    </tr>
    <tr>
        <td>Completed</td>
        <td>COSC-3506</td>
        <td>Software Development</td>
        <td>80</td>
        <td>25F</td>
        <td>3</td>
    </tr>
    <tr>
        <td>Fulfilled</td>
        <td>COSC-9999</td>
        <td>Requirement Placeholder</td>
        <td></td>
        <td></td>
        <td>3</td>
    </tr>
</table>
</body>
</html>
"""


@pytest.fixture(autouse=True)
def reset_application_state():
    app.state.catalog = {}
    app.state.students = {}
    yield


def import_catalog():
    response = client.post(
        "/api/v1/admin/catalog/import",
        files={
            "file": (
                "catalog.html",
                CATALOG_HTML,
                "text/html",
            )
        },
    )

    assert response.status_code == 201
    return response


def import_student(student_id="111"):
    response = client.post(
        f"/api/v1/students/{student_id}/history/import",
        files={
            "file": (
                "student.html",
                TRANSCRIPT_HTML,
                "text/html",
            )
        },
    )

    assert response.status_code == 201
    return response


def test_helper_functions():
    assert normalize_course_code("cosc-3506") == "COSC3506"
    assert normalize_course_code("COSC 3506") == "COSC3506"

    assert parse_credits("3.0") == 3
    assert parse_credits("N/A") == 0

    assert parse_term("23F") < parse_term("24W")
    assert parse_term("26SP") < parse_term("26F")

    codes = extract_course_codes(
        "Requires COSC 2006 and ITEC-3506"
    )

    assert len(codes) == 2


def test_catalog_import_and_lookup():
    response = import_catalog()

    assert response.json()["courses_imported"] == 4

    response = client.get(
        "/api/v1/catalog/courses/COSC-3506"
    )

    assert response.status_code == 200

    course = response.json()

    assert course["course_code"] == "COSC 3506"
    assert course["credits"] == 3
    assert course["prerequisites"] == ["COSC 2007"]
    assert course["cross_listed"] == ["ITEC 3506"]

    response = client.get(
        "/api/v1/catalog/courses/FAKE9999"
    )

    assert response.status_code == 404


def test_history_import_and_profile():
    response = import_student()

    assert response.json()["past_courses_imported"] == 4

    response = client.get(
        "/api/v1/students/111/profile"
    )

    assert response.status_code == 200

    profile = response.json()

    assert set(profile.keys()) == {
        "student_id",
        "history",
        "plan",
    }

    assert profile["student_id"] == "111"
    assert len(profile["history"]) == 4
    assert profile["plan"] == []


def test_history_and_plan_lifecycle():
    import_student()

    plan = {
        "planned_courses": [
            {
                "course_code": "COSC-3506",
                "term": "26F",
            }
        ]
    }

    response = client.post(
        "/api/v1/students/111/plan",
        json=plan,
    )

    assert response.status_code == 200
    assert response.json()["planned_courses_saved"] == 1

    replacement_history = {
        "history": [
            {
                "course_code": "COSC-2007",
                "term": "24F",
                "credits_earned": 3,
                "status": "Completed",
            }
        ]
    }

    response = client.put(
        "/api/v1/students/111/history",
        json=replacement_history,
    )

    assert response.status_code == 200

    replacement_plan = {
        "planned_courses": [
            {
                "course_code": "ITEC-3506",
                "term": "27W",
            }
        ]
    }

    response = client.put(
        "/api/v1/students/111/plan",
        json=replacement_plan,
    )

    assert response.status_code == 200

    response = client.get(
        "/api/v1/students/111/profile"
    )

    profile = response.json()

    assert len(profile["history"]) == 1
    assert len(profile["plan"]) == 1

    response = client.delete(
        "/api/v1/students/111/history"
    )

    assert response.status_code == 200

    response = client.delete(
        "/api/v1/students/111/plan"
    )

    assert response.status_code == 200

    profile = client.get(
        "/api/v1/students/111/profile"
    ).json()

    assert profile["history"] == []
    assert profile["plan"] == []


def test_audit_report():
    import_catalog()
    import_student()

    plan = {
        "planned_courses": [
            {
                "course_code": "COSC-3506",
                "term": "26W",
            },
            {
                "course_code": "ITEC-3506",
                "term": "26F",
            },
        ]
    }

    response = client.post(
        "/api/v1/students/111/plan",
        json=plan,
    )

    assert response.status_code == 200

    response = client.get(
        "/api/v1/students/111/audit-report"
    )

    assert response.status_code == 200

    report = response.json()

    assert report["status"] == "warning"

    assert [
        item["term"]
        for item in report["timeline_validation"]
    ] == ["26W", "26F"]

    assert report["timeline_validation"][0]["errors"][0][
        "type"
    ] == "MISSING_PREREQUISITE"

    assert report["cross_list_violations"][0][
        "type"
    ] == "CROSS_LIST_CONFLICT"

    summary = report["credit_summary"]

    assert summary["total_earned"] == 6
    assert summary["total_planned"] == 6
    assert summary["total_remaining_for_graduation"] == 108

    strict_response = client.get(
        "/api/v1/students/111/audit-report?strict=true"
    )

    assert strict_response.json()["status"] == "failed"


def test_student_isolation_and_unknown_student():
    import_student("111")
    import_student("222")

    client.post(
        "/api/v1/students/111/plan",
        json={
            "planned_courses": [
                {
                    "course_code": "COSC-3506",
                    "term": "26F",
                }
            ]
        },
    )

    profile_111 = client.get(
        "/api/v1/students/111/profile"
    ).json()

    profile_222 = client.get(
        "/api/v1/students/222/profile"
    ).json()

    assert len(profile_111["plan"]) == 1
    assert profile_222["plan"] == []

    response = client.get(
        "/api/v1/students/999/profile"
    )

    assert response.status_code == 404
