"""SNU eTL (Canvas): read the owner's planner and say what is due.

Shared by the ``etl`` router tool and the eTL trigger producer. eTL is the
Canvas instance at ``myetl.snu.ac.kr``; the owner connects it by pasting a
Canvas personal access token into the plugin card (stored under ``snu_etl``).
Read-only: every request is a GET. The token is only ever sent as a header and
never appears in a return value or an exception message.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger(__name__)

__all__ = [
    "BASE_URL",
    "PLUGIN_ID",
    "SEOUL",
    "EtlError",
    "EtlItem",
    "fetch_planner",
    "format_due",
    "is_done",
    "parse_items",
    "status_of",
    "summary_text",
]

PLUGIN_ID: Final[str] = "snu_etl"
BASE_URL: Final[str] = "https://myetl.snu.ac.kr"
SEOUL: Final[ZoneInfo] = ZoneInfo("Asia/Seoul")

#: Planner item types that carry a due date and a submission.
_GRADED_KINDS: Final[frozenset[str]] = frozenset({"assignment", "quiz", "discussion_topic"})
_DONE: Final[frozenset[str]] = frozenset({"excused", "graded", "late", "submitted"})
_MAX_PAGES: Final[int] = 5
SUMMARY_DAYS: Final[int] = 3
OVERDUE_DAYS: Final[int] = 14

Getter = Callable[[str, dict[str, Any] | None], Awaitable[httpx.Response]]


class EtlError(Exception):
    """eTL answered with something other than a readable page."""


@dataclass(frozen=True, slots=True)
class EtlItem:
    id: str
    course: str
    title: str
    kind: str
    due_at: datetime
    status: str
    url: str


def status_of(submissions: dict[str, Any] | bool | None) -> str:
    """One word for a planner item's ``submissions`` block."""
    if not isinstance(submissions, dict):
        return "not_submitted"
    if submissions.get("excused"):
        return "excused"
    if submissions.get("graded"):
        return "graded"
    if submissions.get("submitted"):
        return "late" if submissions.get("late") else "submitted"
    if submissions.get("missing"):
        return "missing"
    return "not_submitted"


def is_done(status: str) -> bool:
    return status in _DONE


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def parse_items(raw: list[dict[str, Any]]) -> list[EtlItem]:
    """The graded, dated planner items, earliest due first."""
    items: list[EtlItem] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("plannable_type") or "")
        due = entry.get("plannable_date")
        if kind not in _GRADED_KINDS or not due:
            continue
        try:
            due_at = _parse_time(str(due))
        except ValueError:
            log.warning("eTL: skipped planner item with an invalid due date")
            continue
        plannable = entry.get("plannable") if isinstance(entry.get("plannable"), dict) else {}
        items.append(
            EtlItem(
                id=f"{kind}:{entry.get('plannable_id')}",
                course=str(entry.get("context_name") or ""),
                title=str(plannable.get("title") or plannable.get("name") or "Untitled"),
                kind=kind,
                due_at=due_at,
                status=status_of(entry.get("submissions")),
                url=f"{BASE_URL}{entry.get('html_url') or ''}",
            )
        )
    items.sort(key=lambda item: item.due_at)
    return items


def format_due(due_at: datetime) -> str:
    return due_at.astimezone(SEOUL).strftime("%a %m/%d %H:%M")


def _line(item: EtlItem, *, overdue: bool = False) -> str:
    state = item.status.replace("_", " ")
    if overdue:
        state = f"{state}, overdue"
    return f"• {format_due(item.due_at)} · {item.course} — {item.title} ({state})"


def summary_text(items: list[EtlItem], now: datetime) -> str:
    """The short daily text: due in the next 3 Seoul days, then anything still open."""
    local_now = now.astimezone(SEOUL)
    window_end = (local_now + timedelta(days=SUMMARY_DAYS)).replace(
        hour=23, minute=59, second=59, microsecond=999999
    )
    overdue_from = now - timedelta(days=OVERDUE_DAYS)
    soon = [i for i in items if now <= i.due_at <= window_end]
    soon_ids = {i.id for i in soon}
    open_items = [
        i for i in items
        if i.id not in soon_ids and not is_done(i.status) and i.due_at >= overdue_from
    ]
    lines: list[str] = []
    if soon:
        lines.append("eTL — due in the next 3 days:")
        lines.extend(_line(i) for i in soon)
    if open_items:
        lines.append("Not submitted yet:")
        lines.extend(_line(i, overdue=i.due_at < now) for i in open_items)
    if not lines:
        return "Nothing due in the next 3 days, and nothing left unsubmitted."
    return "\n".join(lines)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


async def fetch_planner(get: Getter, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Every planner item between ``start`` and ``end`` (at most five pages)."""
    url: str | None = f"{BASE_URL}/api/v1/planner/items"
    params: dict[str, Any] | None = {
        "start_date": _iso(start), "end_date": _iso(end), "per_page": 100,
    }
    out: list[dict[str, Any]] = []
    for _ in range(_MAX_PAGES):
        if url is None:
            break
        response = await get(url, params)
        if response.status_code != 200:
            raise EtlError(f"eTL: HTTP {response.status_code}")
        page = response.json()
        if not isinstance(page, list):
            raise EtlError("eTL: planner page must be a JSON list")
        out.extend(e for e in page if isinstance(e, dict))
        url = (response.links.get("next") or {}).get("url")
        params = None
    return out
