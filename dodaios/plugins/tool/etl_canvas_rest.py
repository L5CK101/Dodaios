"""etl tool — the owner's SNU eTL (Canvas) assignments: what is due and what is submitted.

Wired like ``vercel_rest``: the marketplace's PAT-paste flow stores the owner's
Canvas personal access token in the credential store (key ``snu_etl``); this
tool reads it and calls the eTL REST API directly. Parsing and the summary text
live in ``etl_canvas`` so the eTL trigger producer says the same thing.

Router-tier tool, risk_tier ``monitor``: read-only (GET only). It never
submits work or changes anything in eTL.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from dodaios.core.branding import HTTP_USER_AGENT
from dodaios.core.protocols import ExecutionContext, ToolResult

from .etl_canvas import (
    BASE_URL,
    OVERDUE_DAYS,
    PLUGIN_ID,
    EtlError,
    EtlItem,
    fetch_planner,
    format_due,
    is_done,
    parse_items,
    summary_text,
)

log = logging.getLogger(__name__)

_NOT_CONNECTED = "SNU eTL is not connected — connect it in the Plugins view."
_REJECTED = "SNU eTL rejected the token — reconnect SNU eTL in the Plugins view."
_ACTIONS = ["upcoming", "unsubmitted", "summary", "courses"]
#: How far ahead ``unsubmitted`` and ``summary`` look.
_AHEAD_DAYS = 60


class _Rejected(Exception):
    """eTL answered 401: the token was revoked or expired."""


def _default_token_provider() -> str | None:
    from dodaios.marketplace.token_store import TokenStore

    tokens = TokenStore().load(PLUGIN_ID)
    return tokens.access if tokens is not None else None


def _view(item: EtlItem) -> dict[str, str]:
    return {
        "course": item.course,
        "title": item.title,
        "due": format_due(item.due_at),
        "status": item.status,
        "url": item.url,
    }


def _list_text(items: list[EtlItem], empty: str) -> str:
    if not items:
        return empty
    return "\n".join(
        f"• {format_due(i.due_at)} · {i.course} — {i.title} ({i.status.replace('_', ' ')})"
        for i in items
    )


class EtlCanvasTool:
    name: str = "etl"
    risk_tier: str = "monitor"
    description: str = (
        "List the user's SNU eTL (Canvas) assignments: what is due, what is not "
        "submitted yet, a short summary, or their courses. Use for 'what's due on "
        "eTL', 'did I submit HW3', 'eTL summary'. Actions: upcoming (next `days`, "
        "default 14), unsubmitted, summary, courses. Read-only. Requires the SNU eTL "
        "plugin to be connected in the Plugins view."
    )
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": _ACTIONS, "default": "upcoming"},
            "days": {
                "type": "integer",
                "default": 14,
                "description": "how many days ahead to list (upcoming)",
            },
        },
        "required": ["action"],
    }

    def __init__(
        self,
        access_token_provider: Callable[[], str | None] | None = None,
        transport: Any | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        from ._http_pool import HttpClientPool

        self._token_provider = access_token_provider or _default_token_provider
        self._clock = clock or (lambda: datetime.now(UTC))
        self._pool = HttpClientPool(transport=transport)

    # -- internal helpers ---------------------------------------------------

    def _bearer(self) -> dict[str, str] | None:
        token = self._token_provider()
        if not token:
            return None
        return {"Authorization": f"Bearer {token}", "User-Agent": HTTP_USER_AGENT}

    def _getter(self, headers: dict[str, str]) -> Callable[..., Any]:
        async def get(url: str, params: dict[str, Any] | None) -> httpx.Response:
            response = await self._pool.client().get(url, params=params, headers=headers)
            if response.status_code == 401:
                raise _Rejected
            return response

        return get

    async def _items(self, headers: dict[str, str], now: datetime) -> list[EtlItem]:
        raw = await fetch_planner(
            self._getter(headers),
            now - timedelta(days=OVERDUE_DAYS),
            now + timedelta(days=_AHEAD_DAYS),
        )
        return parse_items(raw)

    async def _run(
        self, action: str, args: dict[str, Any], headers: dict[str, str]
    ) -> dict[str, Any]:
        now = self._clock()
        if action == "courses":
            response = await self._getter(headers)(
                f"{BASE_URL}/api/v1/courses", {"enrollment_state": "active", "per_page": 100}
            )
            if response.status_code != 200:
                raise EtlError(f"eTL: HTTP {response.status_code}")
            body = response.json()
            names = [
                str(c["name"]) for c in body if isinstance(c, dict) and c.get("name")
            ]
            return {"courses": names, "text": "\n".join(names) or "No active courses on eTL."}
        items = await self._items(headers, now)
        if action == "summary":
            return {"items": [_view(i) for i in items], "text": summary_text(items, now)}
        if action == "unsubmitted":
            chosen = [i for i in items if not is_done(i.status)]
            return {
                "items": [_view(i) for i in chosen],
                "text": _list_text(chosen, "Nothing left unsubmitted."),
            }
        days = max(1, int(args.get("days") or 14))
        end = now + timedelta(days=days)
        chosen = [i for i in items if now <= i.due_at <= end]
        return {
            "items": [_view(i) for i in chosen],
            "text": _list_text(chosen, f"Nothing due on eTL in the next {days} days."),
        }

    # -- Tool protocol ------------------------------------------------------

    async def execute(self, args: dict[str, Any], ctx: ExecutionContext) -> ToolResult:
        action = str(args.get("action") or "upcoming").strip()
        if action not in _ACTIONS:
            return ToolResult(success=False, output=None, error=f"unknown action {action!r}")
        headers = self._bearer()
        if headers is None:
            return ToolResult(success=False, output=None, error=_NOT_CONNECTED)
        try:
            out = await self._run(action, args, headers)
        except _Rejected:
            log.warning("eTL: skipped tool action because the token was rejected")
            return ToolResult(success=False, output=None, error=_REJECTED)
        except EtlError as exc:
            log.warning("eTL: skipped tool action because the response was not a readable page")
            return ToolResult(success=False, output=None, error=str(exc))
        except httpx.HTTPError as exc:
            log.warning("eTL: skipped tool action because the HTTP request failed")
            error = f"eTL is unreachable ({type(exc).__name__})"
            return ToolResult(success=False, output=None, error=error)
        return ToolResult(success=True, output=out)
