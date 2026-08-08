"""Notice when the pipeline goes quiet, and put it back.

The failure this exists for
---------------------------
On 2026-08-08 Alchemy stopped delivering for twelve and a half hours. The
webhook was still registered, still ``is_active: true``, still pointing at the
right URL. The service had not restarted. Nothing errored anywhere. The only
symptom was silence — and silence is exactly what "none of the watched wallets
bought anything" looks like, so the UI stayed green and the operator found out
by asking. A re-sync brought it straight back. The same thing had happened for
six hours four days earlier: seventeen hours blind out of seven days.

The existing self-healing does not cover this. That one recreates a webhook
Alchemy has *deleted*, which announces itself with a 404. This failure has no
error to catch.

How silence is told apart from quiet
------------------------------------
Measured on the live stream: deliveries arrive roughly every twelve seconds —
Ethereum's block time — and across a full retained window the largest gap
between consecutive deliveries was fourteen seconds. Quiet hours are slower,
around one a minute, so the default of twenty minutes is between twenty and a
hundred missed deliveries. Nothing normal looks like that.

What it does about it
---------------------
Re-sync first and shout later. A re-sync is cheap, idempotent, and is what
demonstrably fixes this, so it runs at the twenty-minute mark without
bothering anyone. Only if the pipeline is *still* silent well after that does
a message go out — which means a notification always carries the information
that automatic recovery was already tried and failed.

Retries back off. A provider outage lasting a day should not mean a hundred
and forty re-sync attempts against an API that is not answering.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from . import db, monitor
from .config import settings

log = logging.getLogger(__name__)

#: When the process started. Used as a floor for the silence clock so a deploy
#: cannot alarm on a stale timestamp before the first delivery has arrived.
#: Costs up to one silence window of delay after a restart, which is the right
#: trade against crying wolf on every deploy.
STARTED_AT = datetime.now(timezone.utc)

#: Cap on the backoff between re-syncs during one outage.
MAX_COOLDOWN_MINUTES = 60


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment


# ------------------------------------------------------------------- settings


def _setting(name: str, cast, fallback):
    stored = db.get_setting(name)
    if stored is None:
        return fallback
    try:
        return cast(stored)
    except (TypeError, ValueError):
        return fallback


def enabled() -> bool:
    stored = db.get_setting("watchdog_enabled")
    if stored is None:
        return settings.watchdog_enabled
    return stored not in ("0", "false", "False", "")


def silence_minutes() -> int:
    return _setting("watchdog_silence_minutes", int, settings.watchdog_silence_minutes)


def alert_minutes() -> int:
    return _setting("watchdog_alert_minutes", int, settings.watchdog_alert_minutes)


def resync_cooldown_minutes() -> int:
    return _setting(
        "watchdog_resync_cooldown_minutes", int, settings.watchdog_resync_cooldown_minutes
    )


# --------------------------------------------------------------------- state


async def _resync() -> dict[str, Any]:
    """Re-register the address list — the same call the Sync button makes.

    Imported here rather than at module scope because ``main`` imports this
    module; at call time main is long since loaded. Tests replace this
    function wholesale, so nothing needs a live Alchemy account.
    """
    from .main import sync_realtime

    return await sync_realtime()


def _reference_time(last_delivery: str | None) -> datetime:
    """The moment the pipeline was last known to be alive.

    When a delivery has ever arrived, its timestamp is the truth — including
    across a restart. Flooring this at process start was tempting as a way to
    stay quiet during a deploy, but it makes a twelve-hour outage report
    itself as twenty minutes, and understating the failure is the exact habit
    this module exists to break.

    The floor applies only when nothing has *ever* arrived, so a fresh install
    whose webhook was created a minute ago is not immediately called broken.
    """
    return _parse(last_delivery) or STARTED_AT


def status() -> dict[str, Any]:
    """Everything needed to answer "is it working" without side effects."""
    last = db.last_delivery_at()
    silent_for = (_utcnow() - _reference_time(last)).total_seconds()
    live = bool(db.realtime_chains())
    return {
        "enabled": enabled(),
        "watching": live and bool(db.get_setting("alchemy_auth_token")),
        "last_delivery_at": last,
        "silent_for_minutes": round(silent_for / 60, 1),
        "silence_threshold_minutes": silence_minutes(),
        "alert_threshold_minutes": alert_minutes(),
        # The honest headline. "Healthy" is a claim about the last few minutes,
        # not about the day, so the outage list sits beside it.
        "healthy": silent_for < silence_minutes() * 60,
        "outage": db.open_outage(),
        "recent_outages": db.list_outages(limit=10),
    }


# --------------------------------------------------------------------- check


async def check() -> dict[str, Any]:
    """One pass of the state machine. Safe to call every sweep.

    Returns what it decided, so the sweep log and the tests can both see the
    reasoning rather than inferring it from side effects.
    """
    if not enabled():
        return {"checked": False, "reason": "disabled"}

    # Nothing live means nothing should be arriving, and an empty pipeline is
    # not a broken one. Any outage still open ends quietly here rather than
    # being reported as fixed — nobody was told it started.
    if not db.realtime_chains():
        outage = db.open_outage()
        if outage:
            db.resolve_outage(outage["id"], _iso(_utcnow()))
        return {"checked": False, "reason": "no live watchlists"}

    if not db.get_setting("alchemy_auth_token"):
        return {"checked": False, "reason": "no alchemy token"}

    now = _utcnow()
    last = db.last_delivery_at()
    silent_for = (now - _reference_time(last)).total_seconds()
    threshold = silence_minutes() * 60

    if silent_for < threshold:
        outage = db.open_outage()
        if outage and db.resolve_outage(outage["id"], _iso(now)):
            # Only announce the recovery if the outage was announced. A
            # self-healed blip nobody heard about needs no all-clear.
            if outage.get("alerted_at"):
                await _notify_recovered(outage, last)
            log.info("webhook deliveries recovered after %s resync(s)",
                     outage.get("resyncs", 0))
            return {"checked": True, "healthy": True, "recovered": True}
        return {"checked": True, "healthy": True}

    # --- silent for longer than anything normal looks like ---
    outage = db.start_outage(detected_at=_iso(now), last_delivery_at=last)
    acted: list[str] = []

    # Back off between attempts: doubling from the base cooldown, capped, so a
    # provider-side outage lasting a day is a handful of attempts, not
    # hundreds against an API that is plainly not answering.
    cooldown = min(
        resync_cooldown_minutes() * (2 ** int(outage.get("resyncs") or 0)),
        MAX_COOLDOWN_MINUTES,
    )
    last_try = _parse(outage.get("last_resync_at"))
    if last_try is None or (now - last_try) >= timedelta(minutes=cooldown):
        try:
            await _resync()
            db.record_outage_resync(outage["id"], _iso(now))
            acted.append("resynced")
            log.warning(
                "no webhook delivery for %.0f minutes — re-synced addresses",
                silent_for / 60,
            )
        except Exception:
            # A failed re-sync still counts as an attempt, or a provider that
            # always errors would be retried every single sweep.
            db.record_outage_resync(outage["id"], _iso(now))
            acted.append("resync failed")
            log.exception("watchdog re-sync failed")

    if silent_for >= alert_minutes() * 60 and db.mark_outage_alerted(outage["id"], _iso(now)):
        await _notify_down(silent_for, outage, last)
        acted.append("alerted")

    return {
        "checked": True,
        "healthy": False,
        "silent_for_minutes": round(silent_for / 60, 1),
        "outage_id": outage["id"],
        "acted": acted,
    }


# ------------------------------------------------------------- notifications


async def _notify_down(silent_for: float, outage: dict[str, Any], last: str | None) -> None:
    attempts = int(outage.get("resyncs") or 0)
    await monitor.send_watchdog_notification(
        "\n".join(
            [
                "DICE is not receiving webhook deliveries.",
                f"  silent for {silent_for / 60:.0f} minutes"
                + (f", last one {last}" if last else ""),
                f"  re-synced the address list {attempts} time(s) — still nothing",
                "  No signal can fire while this lasts.",
            ]
        )
    )


async def _notify_recovered(outage: dict[str, Any], last: str | None) -> None:
    started = _parse(outage.get("last_delivery_at")) or _parse(outage.get("detected_at"))
    blind = ""
    if started:
        blind = f" after {(_utcnow() - started).total_seconds() / 3600:.1f} hours"
    await monitor.send_watchdog_notification(
        f"Webhook deliveries are arriving again{blind}."
    )


__all__ = [
    "alert_minutes",
    "check",
    "enabled",
    "resync_cooldown_minutes",
    "silence_minutes",
    "status",
]
