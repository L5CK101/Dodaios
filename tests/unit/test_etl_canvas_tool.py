"""The ``etl`` router tool and the SNU eTL plugin card.

eTL is a fake HTTP transport; no network and no real token is involved.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx

from dodaios.core.protocols import ExecutionContext
from dodaios.plugins.tool.etl_canvas import parse_items, summary_text
from dodaios.plugins.tool.etl_canvas_rest import EtlCanvasTool
from tests.unit.test_etl_canvas import planner_item

NOW = datetime(2026, 11, 22, 0, 0, tzinfo=UTC)  # Sun 11/22 09:00 KST
SEED = Path(__file__).resolve().parents[2] / "dodaios" / "marketplace" / "seed_catalog.json"


def _ctx() -> ExecutionContext:
    return ExecutionContext(trace_id=uuid4(), user_utterance="", config={}, memory_read=None)


PLANNER = [
    planner_item(1, due="2026-11-24T14:59:59Z", title="HW3"),
    planner_item(
        2, due="2026-11-23T00:00:00Z", title="Quiz1", kind="quiz", subs={"submitted": True}
    ),
    planner_item(3, due="2026-11-27T00:00:00Z", title="Essay"),
    planner_item(4, due="2026-11-18T00:00:00Z", title="Lab2", subs={"missing": True}),
]


class Fake:
    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"errors": [{"message": "nope"}]})
        if request.url.path == "/api/v1/planner/items":
            return httpx.Response(200, json=PLANNER)
        if request.url.path == "/api/v1/courses":
            courses = [{"id": 1, "name": "Algorithms"}, {"id": 2, "name": "Korean History"}]
            return httpx.Response(200, json=courses)
        return httpx.Response(404, json={})


def _tool(fake: Fake, token: str | None = "tok") -> EtlCanvasTool:  # noqa: S107 - fake token
    return EtlCanvasTool(
        access_token_provider=lambda: token,
        transport=httpx.MockTransport(fake.handler),
        clock=lambda: NOW,
    )


async def test_tool_upcoming_lists_items_with_status() -> None:
    res = await _tool(Fake()).execute({"action": "upcoming"}, _ctx())
    assert res.success, res.error
    items = res.output["items"]
    assert [i["title"] for i in items] == ["Quiz1", "HW3", "Essay"]
    assert [i["status"] for i in items] == ["submitted", "not_submitted", "not_submitted"]
    assert items[1]["due"] == "Tue 11/24 23:59"
    assert items[1]["url"] == "https://myetl.snu.ac.kr/courses/1/assignments/1"
    assert "HW3" in res.output["text"]


async def test_tool_upcoming_respects_days() -> None:
    res = await _tool(Fake()).execute({"action": "upcoming", "days": 3}, _ctx())
    assert [i["title"] for i in res.output["items"]] == ["Quiz1", "HW3"]


async def test_tool_unsubmitted_excludes_done() -> None:
    res = await _tool(Fake()).execute({"action": "unsubmitted"}, _ctx())
    assert [i["title"] for i in res.output["items"]] == ["Lab2", "HW3", "Essay"]


async def test_tool_summary_matches_summary_text() -> None:
    res = await _tool(Fake()).execute({"action": "summary"}, _ctx())
    assert res.output["text"] == summary_text(parse_items(PLANNER), NOW)


async def test_tool_courses_lists_active_course_names() -> None:
    fake = Fake()
    res = await _tool(fake).execute({"action": "courses"}, _ctx())
    assert res.output["courses"] == ["Algorithms", "Korean History"]
    assert fake.requests[0].url.params["enrollment_state"] == "active"


async def test_tool_not_connected() -> None:
    res = await _tool(Fake(), token=None).execute({"action": "upcoming"}, _ctx())
    assert not res.success
    assert "not connected" in (res.error or "")


async def test_tool_401_says_reconnect_without_token() -> None:
    tool = _tool(Fake(status=401), token="secret-tok")  # noqa: S106 - fake token
    res = await tool.execute({"action": "upcoming"}, _ctx())
    assert not res.success
    assert "reconnect" in (res.error or "")
    assert "secret-tok" not in (res.error or "")


async def test_tool_unknown_action() -> None:
    res = await _tool(Fake()).execute({"action": "submit"}, _ctx())
    assert not res.success


async def test_tool_sends_bearer_and_only_gets() -> None:
    fake = Fake()
    tool = _tool(fake)
    for action in ("upcoming", "unsubmitted", "summary", "courses"):
        await tool.execute({"action": action}, _ctx())
    assert fake.requests
    assert all(r.method == "GET" for r in fake.requests)
    assert all(r.headers["Authorization"] == "Bearer tok" for r in fake.requests)
    assert all(r.url.host == "myetl.snu.ac.kr" for r in fake.requests)


def test_tool_identity() -> None:
    assert EtlCanvasTool.name == "etl"
    assert EtlCanvasTool.risk_tier == "monitor"
    assert EtlCanvasTool.schema["properties"]["action"]["enum"] == [
        "upcoming", "unsubmitted", "summary", "courses",
    ]


def test_catalog_card_is_pat_paste() -> None:
    cards = json.loads(SEED.read_text(encoding="utf-8"))["plugins"]
    card = next(c for c in cards if c["id"] == "snu_etl")
    assert card["display_name"] == "SNU eTL"
    assert card["auth"]["mode"] == "pat_paste"
    assert card["auth"]["validation_endpoint"] == "https://myetl.snu.ac.kr/api/v1/users/self"
    assert card["auth"]["token_creation_url"] == "https://myetl.snu.ac.kr/profile/settings"  # noqa: S105
    assert "mcp_server" not in card


def test_tool_is_registered_as_entry_point() -> None:
    text = (SEED.parents[2] / "pyproject.toml").read_text(encoding="utf-8")
    assert 'etl = "dodaios.plugins.tool.etl_canvas_rest:EtlCanvasTool"' in text
