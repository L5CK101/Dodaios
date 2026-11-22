"""The eTL poller: quiet first run, due-date changes, new work and one 09:00 summary a day.

eTL is a fake HTTP transport; no model and no real token is involved.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from dodaios.lead_triggers import SourceRule, TriggerEvent, TriggerSettings
from dodaios.lead_triggers.event import SOURCES
from dodaios.lead_triggers.filter import Stage1
from dodaios.lead_triggers.producers.base import ProviderError
from dodaios.lead_triggers.producers.etl import EtlProducer
from dodaios.lead_triggers.settings import PRODUCERS
from tests.unit.lead_triggers.test_producers import Fake, ok
from tests.unit.test_etl_canvas import planner_item

NOW_9 = datetime(2026, 11, 22, 0, 30, tzinfo=UTC).timestamp()  # Sun 11/22 09:30 KST
NOW_8 = datetime(2026, 11, 21, 23, 30, tzinfo=UTC).timestamp()  # Sun 11/22 08:30 KST
TODAY = "2026-11-22"
YESTERDAY = "2026-11-21"


def _fake(items: list[dict[str, Any]]) -> Fake:
    fake = Fake()
    fake.routes["/api/v1/planner/items"] = ok(items)
    return fake


def _producer(items: list[dict[str, Any]]) -> EtlProducer:
    return EtlProducer(_fake(items).api("snu_etl"))


def _summaries(events: list[Any]) -> list[Any]:
    return [e for e in events if e.thread_key.startswith("summary:")]


async def test_first_poll_is_quiet_and_records_due_dates() -> None:
    poll = await _producer([planner_item(1)]).poll({}, NOW_9)
    assert poll.events == []
    assert poll.cursor["due"] == {"assignment:1": "2026-11-24T14:59:59+00:00"}
    assert poll.min_interval_s == 1800


async def test_first_poll_after_9_marks_summary_day_without_sending() -> None:
    poll = await _producer([planner_item(1)]).poll({}, NOW_9)
    assert poll.cursor["summary_day"] == TODAY


async def test_first_poll_before_9_leaves_summary_for_later() -> None:
    poll = await _producer([planner_item(1)]).poll({}, NOW_8)
    assert poll.events == []
    assert poll.cursor.get("summary_day", "") == ""


async def test_due_date_change_emits_one_event() -> None:
    cursor = {"due": {"assignment:1": "2026-11-24T14:59:59+00:00"}, "summary_day": TODAY}
    producer = _producer([planner_item(1, due="2026-11-26T14:59:59Z")])
    poll = await producer.poll(cursor, NOW_9)
    assert len(poll.events) == 1
    event = poll.events[0]
    assert event.source == "etl" and event.scope == "Algorithms"
    # The change stamp keeps a second move of the same deadline from being
    # dropped by the intake's thread cooldown.
    assert event.thread_key == "assignment:1@2026-11-26T14:59:59+00:00"
    assert event.summary.startswith("Due date changed: Algorithms — HW3:")
    assert "Tue 11/24 23:59 → Thu 11/26 23:59" in event.summary
    assert event.raw_ref == "https://myetl.snu.ac.kr/courses/1/assignments/1"
    again = await producer.poll(poll.cursor, NOW_9 + 1800)
    assert again.events == []


async def test_new_assignment_emits_event() -> None:
    cursor = {"due": {"assignment:1": "2026-11-24T14:59:59+00:00"}, "summary_day": TODAY}
    items = [planner_item(1), planner_item(2, title="HW4", due="2026-11-28T14:59:59Z")]
    poll = await _producer(items).poll(cursor, NOW_9)
    assert [e.summary for e in poll.events] == ["New on eTL: Algorithms — HW4, due Sat 11/28 23:59"]
    assert "assignment:2" in poll.cursor["due"]


async def test_removed_assignment_is_dropped_silently() -> None:
    cursor = {
        "due": {
            "assignment:1": "2026-11-24T14:59:59+00:00",
            "assignment:9": "2026-11-25T00:00:00+00:00",
        },
        "summary_day": TODAY,
    }
    poll = await _producer([planner_item(1)]).poll(cursor, NOW_9)
    assert poll.events == []
    assert set(poll.cursor["due"]) == {"assignment:1"}


async def test_summary_once_per_seoul_day_after_9() -> None:
    cursor = {"due": {"assignment:1": "2026-11-24T14:59:59+00:00"}, "summary_day": YESTERDAY}
    producer = _producer([planner_item(1)])
    early = await producer.poll(cursor, NOW_8)
    assert _summaries(early.events) == []
    late = await producer.poll(early.cursor, NOW_9)
    summaries = _summaries(late.events)
    assert len(summaries) == 1
    assert summaries[0].thread_key == f"summary:{TODAY}"
    assert summaries[0].source == "etl" and summaries[0].scope == "eTL"
    assert "HW3" in summaries[0].summary
    assert late.cursor["summary_day"] == TODAY
    later = await producer.poll(late.cursor, NOW_9 + 1800)
    assert _summaries(later.events) == []


async def test_summary_when_nothing_is_due() -> None:
    cursor = {"due": {}, "summary_day": YESTERDAY}
    poll = await _producer([]).poll(cursor, NOW_9)
    assert [e.summary for e in poll.events] == [
        "Nothing due in the next 3 days, and nothing left unsubmitted."
    ]


async def test_provider_error_maps_to_provider_error() -> None:
    fake = Fake()
    fake.routes["/api/v1/planner/items"] = httpx.Response(500, json={})
    with pytest.raises(ProviderError):
        await EtlProducer(fake.api("snu_etl")).poll({}, NOW_9)


async def test_requests_go_to_etl_with_a_window() -> None:
    fake = _fake([planner_item(1)])
    await EtlProducer(fake.api("snu_etl")).poll({}, NOW_9)
    request = fake.requests[0]
    assert request.url.host == "myetl.snu.ac.kr" and request.method == "GET"
    assert request.url.params["start_date"].startswith("2026-11-08")
    assert request.url.params["end_date"].startswith("2027-01-21")


def test_etl_is_a_known_source_and_producer() -> None:
    assert "etl" in SOURCES
    assert "snu_etl" in PRODUCERS
    assert TriggerSettings().producer_enabled("snu_etl")
    producer = EtlProducer(Fake().api("snu_etl"))
    assert producer.id == "snu_etl" and producer.source == "etl"


# --------------------------------------------------------------- final-review fixes


async def test_new_then_changed_then_changed_again_all_pass_the_intake() -> None:
    settings = TriggerSettings(sources={"etl": SourceRule(mode="candidate")})
    stage = Stage1(clock=lambda: NOW_9)
    cursor: dict[str, Any] = {"due": {}, "summary_day": TODAY}
    verdicts = []
    for due in ("2026-11-24T14:59:59Z", "2026-11-26T14:59:59Z", "2026-11-27T14:59:59Z"):
        poll = await _producer([planner_item(1, due=due)]).poll(cursor, NOW_9)
        cursor = poll.cursor
        verdicts += [stage.verdict(e, settings) for e in poll.events]
    assert verdicts == ["candidate", "candidate", "candidate"]


async def test_daily_summary_is_one_short_line_that_survives_storage() -> None:
    cursor = {"due": {}, "summary_day": YESTERDAY}
    items = [
        planner_item(i, title=f"Homework number {i}", due="2026-11-24T14:59:59Z")
        for i in range(1, 12)
    ]
    poll = await _producer(items).poll(cursor, NOW_9)
    (summary,) = _summaries(poll.events)
    assert len(summary.summary) <= 300 and "\n" not in summary.summary
    assert TriggerEvent.from_dict(summary.to_dict()).summary == summary.summary
    assert "11 due in the next 3 days" in summary.summary
    assert "Homework number 1" in summary.summary
    assert "eTL summary" in summary.summary


async def test_empty_reply_after_known_items_is_skipped() -> None:
    cursor = {"due": {"assignment:1": "2026-11-24T14:59:59+00:00"}, "summary_day": TODAY}
    empty = await _producer([]).poll(cursor, NOW_9)
    assert empty.events == []
    assert empty.cursor["due"] == cursor["due"]
    back = await _producer([planner_item(1)]).poll(empty.cursor, NOW_9 + 1800)
    assert back.events == []


async def test_many_new_items_fold_into_one_event() -> None:
    cursor = {"due": {"assignment:1": "2026-11-24T14:59:59+00:00"}, "summary_day": TODAY}
    items = [planner_item(1)] + [planner_item(i, title=f"W{i}") for i in range(2, 9)]
    poll = await _producer(items).poll(cursor, NOW_9)
    assert len(poll.events) == 1
    assert poll.events[0].summary.startswith("7 new items on eTL")
    assert len(poll.events[0].summary) <= 300
    assert len(poll.cursor["due"]) == 8
