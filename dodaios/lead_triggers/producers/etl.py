"""SNU eTL (Canvas): due-date changes, new assignments and one 09:00 summary a day.

Polls the owner's Canvas planner every 30 minutes through the pasted token the
SNU eTL plugin holds (``snu_etl``). The cursor remembers each item's due date,
so a changed date or a new item becomes one event. The first poll only records
what is there, so connecting eTL never floods the owner with alerts.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Final

from dodaios.plugins.tool.etl_canvas import (
    OVERDUE_DAYS,
    PLUGIN_ID,
    SEOUL,
    SUMMARY_DAYS,
    EtlError,
    EtlItem,
    fetch_planner,
    format_due,
    is_done,
    parse_items,
)

from ..event import TriggerEvent
from .base import Api, Poll, ProviderError

POLL_INTERVAL_S: Final[float] = 1800.0
AHEAD_DAYS: Final[int] = 60
#: The daily summary goes out on the first poll at or after this Seoul hour.
SUMMARY_HOUR: Final[int] = 9


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _daily_summary(items: list[EtlItem], now: datetime) -> str:
    """Keep counts and the next due items within the stored event's text limit."""
    window_end = (now.astimezone(SEOUL) + timedelta(days=SUMMARY_DAYS)).replace(
        hour=23, minute=59, second=59, microsecond=999999
    )
    soon = [item for item in items if now <= item.due_at <= window_end]
    open_count = sum(
        not is_done(item.status) and item.due_at >= now - timedelta(days=OVERDUE_DAYS)
        for item in items
    )
    if not soon and not open_count:
        return "Nothing due in the next 3 days, and nothing left unsubmitted."
    prefix = f"{len(soon)} due in the next 3 days"
    suffix = f"; {open_count} not submitted. Say 'eTL summary' for the list."
    next_items: list[str] = []
    for item in soon:
        title = " ".join(item.title.split())
        detail = f"{title}, {format_due(item.due_at)}"
        candidate = f"{prefix} (next: {'; '.join([*next_items, detail])}){suffix}"
        if len(candidate) > 300:
            if not next_items:
                available = 300 - len(f"{prefix} (next: , {format_due(item.due_at)}){suffix}")
                next_items.append(f"{title[:available]}, {format_due(item.due_at)}")
            break
        next_items.append(detail)
    if next_items:
        prefix += f" (next: {'; '.join(next_items)})"
    return prefix + suffix


class EtlProducer:
    id = PLUGIN_ID
    source = "etl"

    def __init__(self, api: Api) -> None:
        self._api = api

    async def connected(self) -> bool:
        return await self._api.connected()

    async def _items(self, now_dt: datetime) -> list[EtlItem]:
        try:
            raw = await fetch_planner(
                self._api.get,
                now_dt - timedelta(days=OVERDUE_DAYS),
                now_dt + timedelta(days=AHEAD_DAYS),
            )
        except EtlError as exc:
            raise ProviderError(str(exc)) from exc
        return parse_items(raw)

    def _event(
        self, item: EtlItem, summary: str, *, change_stamp: str | None = None
    ) -> TriggerEvent:
        key = f"{item.id}@{change_stamp}" if change_stamp is not None else item.id
        return TriggerEvent(
            source=self.source, scope=item.course, thread_key=key,
            summary=summary, raw_ref=item.url, ids=(key,),
        )

    async def poll(self, cursor: dict[str, Any], now: float) -> Poll:
        first = not cursor
        now_dt = datetime.fromtimestamp(now, UTC)
        local = now_dt.astimezone(SEOUL)
        today = local.date().isoformat()
        known: dict[str, str] = dict(cursor.get("due") or {})
        summary_day = str(cursor.get("summary_day") or "")
        items = await self._items(now_dt)

        if known and not items:
            return Poll(events=[], cursor=cursor, min_interval_s=POLL_INTERVAL_S)

        new_items = [item for item in items if item.id not in known]
        events: list[TriggerEvent] = []
        if not first and len(new_items) > 5:
            events.append(TriggerEvent(
                source=self.source, scope="eTL", thread_key=f"new:{now}",
                summary=f"{len(new_items)} new items on eTL",
            ))
        due: dict[str, str] = {}
        for item in items:
            stamp = item.due_at.isoformat()
            due[item.id] = stamp
            if first:
                continue
            before = known.get(item.id)
            if before is None and len(new_items) <= 5:
                events.append(self._event(
                    item,
                    f"New on eTL: {item.course} — {item.title}, due {format_due(item.due_at)}",
                ))
            elif before is not None and before != stamp:
                events.append(self._event(
                    item,
                    f"Due date changed: {item.course} — {item.title}: "
                    f"{format_due(_parse(before))} → {format_due(item.due_at)}",
                    change_stamp=stamp,
                ))

        if local.hour >= SUMMARY_HOUR and summary_day != today:
            if not first:
                events.append(TriggerEvent(
                    source=self.source, scope="eTL", thread_key=f"summary:{today}",
                    summary=_daily_summary(items, now_dt),
                ))
            summary_day = today

        return Poll(
            events=events,
            cursor={"due": due, "summary_day": summary_day},
            min_interval_s=POLL_INTERVAL_S,
        )
