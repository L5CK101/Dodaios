"""SNU eTL (Canvas) planner parsing, status mapping and the daily summary text.

Every Canvas reply is made up; no network and no real token is involved.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from dodaios.plugins.tool.etl_canvas import (
    EtlError,
    fetch_planner,
    format_due,
    parse_items,
    status_of,
    summary_text,
)


def planner_item(
    pid: int,
    *,
    kind: str = "assignment",
    due: str | None = "2026-11-24T14:59:59Z",
    course: str = "Algorithms",
    title: str = "HW3",
    subs: dict[str, Any] | bool | None = None,
) -> dict[str, Any]:
    return {
        "plannable_id": pid,
        "plannable_type": kind,
        "plannable_date": due,
        "context_name": course,
        "plannable": {"title": title},
        "html_url": f"/courses/1/assignments/{pid}",
        "submissions": subs if subs is not None else {},
    }


def test_status_of_maps_canvas_flags() -> None:
    assert status_of({"excused": True, "graded": True}) == "excused"
    assert status_of({"graded": True, "submitted": True}) == "graded"
    assert status_of({"submitted": True, "late": True}) == "late"
    assert status_of({"submitted": True}) == "submitted"
    assert status_of({"missing": True}) == "missing"
    assert status_of({}) == "not_submitted"


def test_status_of_false_submissions() -> None:
    assert status_of(False) == "not_submitted"
    assert status_of(None) == "not_submitted"


def test_parse_skips_undated_and_non_graded() -> None:
    raw = [
        planner_item(1),
        planner_item(2, due=None),
        planner_item(3, kind="announcement"),
        planner_item(4, kind="calendar_event"),
        planner_item(5, kind="quiz", due="2026-11-23T00:00:00Z"),
    ]
    items = parse_items(raw)
    assert [i.id for i in items] == ["quiz:5", "assignment:1"]
    assert items[1].url == "https://myetl.snu.ac.kr/courses/1/assignments/1"
    assert items[1].course == "Algorithms" and items[1].title == "HW3"
    assert items[1].due_at == datetime(2026, 11, 24, 14, 59, 59, tzinfo=UTC)


def test_parse_falls_back_to_plannable_name() -> None:
    raw = [planner_item(7)]
    raw[0]["plannable"] = {"name": "Forum post"}
    assert parse_items(raw)[0].title == "Forum post"


def test_format_due_is_seoul_time() -> None:
    assert format_due(datetime(2026, 11, 24, 14, 59, tzinfo=UTC)) == "Tue 11/24 23:59"


def test_summary_uses_seoul_day_boundary() -> None:
    now = datetime(2026, 11, 22, 0, 0, tzinfo=UTC)  # Sun 11/22 09:00 KST
    # Wed 23:59 KST
    inside = parse_items([planner_item(1, due="2026-11-25T14:59:00Z", title="InWindow")])
    outside = parse_items(
        [planner_item(2, due="2026-11-25T15:00:00Z", title="NextDay", subs={"submitted": True})]
    )  # Thu 00:00 KST
    text = summary_text(inside + outside, now)
    assert "InWindow" in text
    assert "NextDay" not in text


def test_summary_lists_unsubmitted_including_recent_overdue() -> None:
    now = datetime(2026, 11, 22, 0, 0, tzinfo=UTC)
    items = parse_items(
        [
            planner_item(1, due="2026-11-30T00:00:00Z", title="Later"),
            planner_item(2, due="2026-11-15T00:00:00Z", title="Overdue", subs={"missing": True}),
            planner_item(3, due="2026-10-01T00:00:00Z", title="Ancient", subs={"missing": True}),
            planner_item(4, due="2026-11-23T00:00:00Z", title="Done", subs={"submitted": True}),
        ]
    )
    text = summary_text(items, now)
    assert "Later" in text and "Overdue" in text
    assert "Ancient" not in text
    assert "Done (submitted)" in text


def test_summary_does_not_repeat_an_item_in_both_sections() -> None:
    now = datetime(2026, 11, 22, 0, 0, tzinfo=UTC)
    items = parse_items([planner_item(1, due="2026-11-23T00:00:00Z", title="Soon")])
    text = summary_text(items, now)
    assert text.count("Soon") == 1
    assert text.startswith("eTL — due in the next 3 days:")


def test_summary_layout() -> None:
    now = datetime(2026, 11, 22, 0, 0, tzinfo=UTC)
    items = parse_items(
        [
            planner_item(1, title="HW3"),
            planner_item(2, due="2026-11-15T00:00:00Z", title="Overdue", subs={"missing": True}),
        ]
    )
    assert summary_text(items, now) == (
        "eTL — due in the next 3 days:\n"
        "• Tue 11/24 23:59 · Algorithms — HW3 (not submitted)\n"
        "Not submitted yet:\n"
        "• Sun 11/15 09:00 · Algorithms — Overdue (missing, overdue)"
    )


def test_summary_when_nothing() -> None:
    assert summary_text([], datetime(2026, 11, 22, tzinfo=UTC)) == (
        "Nothing due in the next 3 days, and nothing left unsubmitted."
    )


def _getter(handler: Any, token: str = "tok") -> Any:  # noqa: S107 - fake token
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def get(url: str, params: dict[str, Any] | None) -> httpx.Response:
        return await client.get(url, params=params, headers={"Authorization": f"Bearer {token}"})

    return get


async def test_fetch_planner_follows_next_links_and_caps_pages() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        page = int(request.url.params.get("page", "1"))
        nxt = f"https://myetl.snu.ac.kr/api/v1/planner/items?page={page + 1}"
        link = {"Link": f'<{nxt}>; rel="next"'}
        return httpx.Response(200, json=[planner_item(page)], headers=link)

    start = datetime(2026, 11, 8, tzinfo=UTC)
    end = datetime(2027, 1, 21, tzinfo=UTC)
    raw = await fetch_planner(_getter(handler), start, end)
    assert len(seen) == 5 and len(raw) == 5
    first = seen[0].url
    assert first.path == "/api/v1/planner/items"
    assert first.params["per_page"] == "100"
    assert first.params["start_date"].startswith("2026-11-08")
    assert first.params["end_date"].startswith("2027-01-21")


async def test_fetch_planner_error_hides_auth() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with pytest.raises(EtlError) as exc:
        await fetch_planner(
            _getter(handler, token="secret-tok"),  # noqa: S106 - fake token
            datetime(2026, 11, 8, tzinfo=UTC),
            datetime(2026, 11, 30, tzinfo=UTC),
        )
    assert "500" in str(exc.value)
    assert "secret-tok" not in str(exc.value)


async def test_fetch_planner_rejects_a_non_list_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errors": [{"message": "unexpected"}]})

    with pytest.raises(EtlError):
        await fetch_planner(
            _getter(handler), datetime(2026, 11, 8, tzinfo=UTC), datetime(2026, 11, 30, tzinfo=UTC)
        )
