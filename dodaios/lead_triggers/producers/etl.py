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
    EtlError,
    EtlItem,
    fetch_planner,
    format_due,
    parse_items,
    summary_text,
)

from ..event import TriggerEvent
from .base import Api, Poll, ProviderError

POLL_INTERVAL_S: Final[float] = 1800.0
AHEAD_DAYS: Final[int] = 60
#: The daily summary goes out on the first poll at or after this Seoul hour.
SUMMARY_HOUR: Final[int] = 9


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


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

    def _event(self, item: EtlItem, summary: str) -> TriggerEvent:
        return TriggerEvent(
            source=self.source, scope=item.course, thread_key=item.id,
            summary=summary, raw_ref=item.url, ids=(item.id,),
        )

    async def poll(self, cursor: dict[str, Any], now: float) -> Poll:
        first = not cursor
        now_dt = datetime.fromtimestamp(now, UTC)
        local = now_dt.astimezone(SEOUL)
        today = local.date().isoformat()
        known: dict[str, str] = dict(cursor.get("due") or {})
        summary_day = str(cursor.get("summary_day") or "")
        items = await self._items(now_dt)

        events: list[TriggerEvent] = []
        due: dict[str, str] = {}
        for item in items:
            stamp = item.due_at.isoformat()
            due[item.id] = stamp
            if first:
                continue
            before = known.get(item.id)
            if before is None:
                events.append(self._event(
                    item, f"New on eTL: {item.course} — {item.title}, due {format_due(item.due_at)}",
                ))
            elif before != stamp:
                events.append(self._event(
                    item,
                    f"Due date changed: {item.course} — {item.title}: "
                    f"{format_due(_parse(before))} → {format_due(item.due_at)}",
                ))

        if local.hour >= SUMMARY_HOUR and summary_day != today:
            if not first:
                events.append(TriggerEvent(
                    source=self.source, scope="eTL", thread_key=f"summary:{today}",
                    summary=summary_text(items, now_dt),
                ))
            summary_day = today

        return Poll(
            events=events,
            cursor={"due": due, "summary_day": summary_day},
            min_interval_s=POLL_INTERVAL_S,
        )
