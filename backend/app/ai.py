"""The two places a language model actually earns its keep in DICE.

Where it is *not* used
----------------------
Detection. Which wallets bought what, how many, inside which window, and
whether that crosses a threshold — that is set arithmetic and SQL. Handing it
to a model would make it slower, more expensive, occasionally wrong, and
worst of all **untestable**: the 300 tests that currently pin signal behaviour
would all become approximately true. Contract safety is likewise a dedicated
service's job (:mod:`app.security`), not a model's opinion about bytecode.

Where it does
-------------
**Triage.** A signal says "GEM, 19 of 180 wallets". Finding out what GEM
actually *is* costs a human five minutes, which on a six-hour-old pool is most
of the edge. The enrichment brief answers that question — with web search, so
it can report what the token claims to be rather than paraphrasing numbers
DICE already showed you.

**Review.** Once :mod:`app.performance` has outcomes and
:mod:`app.wallets` has scores, someone has to read them and say "signals under
six hours old won; over 48 lost; move the filter". That is judgement over a
small table, which is exactly what a model is good at — and it only became
possible once the outcome data existed.

Rules both paths follow
-----------------------
The model never decides whether to buy, never computes a threshold, and never
overrides the liquidity or contract gates. It summarises facts DICE gathered
and proposes changes a human applies. Every call is best-effort and
time-boxed: if the API is slow, absent, or refuses, the signal still fires and
the notification still goes out — just without the extra paragraph.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import anthropic

from . import db
from .config import settings

log = logging.getLogger(__name__)

#: One model for both paths. The enrichment call is short and runs at low
#: effort; the review is long and runs high. Splitting models to save pennies
#: would trade away the judgement that is the entire point of the review.
MODEL = "claude-opus-5"

#: Generous, because it is a ceiling rather than a charge — you pay for what
#: is generated. A tight cap only buys the chance of a truncated answer.
ENRICH_MAX_TOKENS = 8_000
REVIEW_MAX_TOKENS = 16_000

#: Enrichment sits in the webhook request path, and Alchemy retries a delivery
#: that answers too slowly. Past this, the signal goes out unenriched — a
#: late brief is worth less than a duplicate delivery.
ENRICH_TIMEOUT_SECONDS = 12.0

ENRICH_SYSTEM = """\
You brief a crypto trader on a token their wallet-tracking system just \
flagged. They have seconds to decide, and they can already see every number \
below — do not read them back.

Your job is the part they cannot see: what this token actually is. Search for \
it. Report what you find and how solid it looks: is there a real project, how \
old, who is behind it, is the attention organic or manufactured.

Answer in exactly this shape, nothing else:

THEME: <one word — ai, gaming, meme, defi, rwa, infra, privacy, social, \
unknown>
WHAT: <one sentence on what the token is, from what you found>
READ: <one sentence on what the combination of their numbers and your \
findings suggests>
RISK: <one sentence on the single biggest reason this could go wrong>

Hard rules. If search finds nothing about this token, say so plainly in WHAT \
— an unfindable token days after launch is itself the finding, and inventing \
a plausible project is the one failure that would make this worse than \
useless. Never tell them to buy, sell, or size a position; they decide, you \
inform. Distinguish what you verified from what a project claims about \
itself.\
"""

REVIEW_SYSTEM = """\
You are reviewing the measured performance of a wallet-tracking signal system \
and proposing changes to its parameters.

You are reading a small table of outcomes, not guessing. Ground every \
recommendation in a number from the data, name that number, and say how many \
signals it rests on. Where the sample is too small to justify a change, say \
that instead of proposing one — "not enough data yet" is the correct answer \
far more often than it is given, and a confident recommendation from four \
data points is worse than silence.

Prefer one change that matters to five that might. The operator applies these \
by hand, so each one costs their attention.\
"""

#: The review returns a fixed shape so the UI can render it without parsing
#: prose. Constraint keywords (minLength, maximum, …) are unsupported by the
#: structured-output schema compiler and are deliberately absent.
REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "description": (
                "Two sentences on what the data does and does not yet show. "
                "Say plainly when the sample is too small to conclude anything."
            ),
        },
        "confidence": {
            "type": "string",
            "enum": ["none", "low", "medium", "high"],
            "description": (
                "How much the data supports any conclusion at all. 'none' when "
                "there are too few scored signals to say anything."
            ),
        },
        "recommendations": {
            "type": "array",
            "description": (
                "Changes worth making, most valuable first. Empty when the "
                "data does not yet justify one."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "setting": {
                        "type": "string",
                        "description": (
                            "What to change, in the operator's words — e.g. "
                            "'max pool age', 'pool %', 'drop these wallets'."
                        ),
                    },
                    "change": {
                        "type": "string",
                        "description": "The specific change, including the new value.",
                    },
                    "evidence": {
                        "type": "string",
                        "description": (
                            "The numbers behind it, including how many signals "
                            "they rest on."
                        ),
                    },
                },
                "required": ["setting", "change", "evidence"],
                "additionalProperties": False,
            },
        },
        "watch_next": {
            "type": "string",
            "description": (
                "The one thing to collect more of before the next review, and why."
            ),
        },
    },
    "required": ["verdict", "confidence", "recommendations", "watch_next"],
    "additionalProperties": False,
}


class AIUnavailable(RuntimeError):
    """No key, disabled, or the API could not be reached. Never fatal."""


def api_key() -> str | None:
    return db.get_setting("anthropic_api_key") or settings.anthropic_api_key


def enabled() -> bool:
    """Enrichment runs only when both a key and the toggle are present."""
    stored = db.get_setting("ai_enrichment")
    on = (
        settings.ai_enrichment
        if stored is None
        else stored.strip().lower() in ("1", "true", "yes", "on")
    )
    return bool(on and api_key())


def _client() -> anthropic.AsyncAnthropic:
    key = api_key()
    if not key:
        raise AIUnavailable("No Anthropic API key saved.")
    return anthropic.AsyncAnthropic(api_key=key)


def _text(response: Any) -> str:
    """Concatenate the text blocks of a response, ignoring tool bookkeeping."""
    return "\n".join(
        block.text for block in response.content if getattr(block, "type", "") == "text"
    ).strip()


async def _ask(client: anthropic.AsyncAnthropic, **kwargs: Any) -> Any:
    """One request, resuming a server-tool pause once.

    A web search can exhaust the server-side tool loop and come back
    ``pause_turn`` — a complete-looking response that is actually half an
    answer. Resuming once is enough for a brief; a second pause means the
    search is wandering and the partial answer is better than more latency.
    """
    response = await client.messages.create(**kwargs)
    if response.stop_reason != "pause_turn":
        return response
    resumed = dict(kwargs)
    resumed["messages"] = [
        *kwargs["messages"],
        {"role": "assistant", "content": response.content},
    ]
    return await client.messages.create(**resumed)


def parse_brief(text: str) -> dict[str, Any]:
    """Split the four labelled lines out of the brief.

    Tolerant on purpose: a model that returns prose instead of the shape still
    yields a usable brief rather than nothing, because the raw text is kept
    either way and the labels are only used for display.
    """
    fields = {"theme": None, "what": "", "read": "", "risk": ""}
    for line in text.splitlines():
        label, _, rest = line.partition(":")
        key = label.strip().lower()
        if key in fields and rest.strip():
            fields[key] = rest.strip()
    if fields["theme"]:
        # One word, lowercased — it is a grouping key, not a sentence.
        fields["theme"] = fields["theme"].split()[0].strip(".,").lower()
    return {**fields, "text": text}


async def brief_token(
    *,
    chain: str,
    token_address: str,
    symbol: str | None,
    facts: dict[str, Any],
) -> dict[str, Any]:
    """Research a signalled token and return a short brief.

    Raises :class:`AIUnavailable` on any failure — callers treat a missing
    brief as normal, because a signal without one is still a signal.
    """
    client = _client()
    lines = [
        f"Chain: {chain}",
        f"Token: {symbol or 'unknown symbol'} ({token_address})",
    ]
    for label, value in facts.items():
        if value not in (None, "", [], {}):
            lines.append(f"{label}: {value}")

    try:
        response = await _ask(
            client,
            model=MODEL,
            max_tokens=ENRICH_MAX_TOKENS,
            # Low effort: this is a search-and-summarise task with a fixed
            # output shape, and the latency sits in a webhook request path.
            output_config={"effort": "low"},
            system=ENRICH_SYSTEM,
            tools=[
                {"type": "web_search_20260209", "name": "web_search", "max_uses": 5}
            ],
            messages=[{"role": "user", "content": "\n".join(lines)}],
        )
    except (anthropic.APIStatusError, anthropic.APIConnectionError) as error:
        raise AIUnavailable(str(error)) from error
    finally:
        await client.close()

    if response.stop_reason == "refusal":
        # A safety decline is a valid outcome, not a bug. Surface it as "no
        # brief" rather than letting a caller read an empty content list.
        raise AIUnavailable("the model declined to answer")

    text = _text(response)
    if not text:
        raise AIUnavailable("the model returned nothing")
    return parse_brief(text)


async def review(payload: dict[str, Any]) -> dict[str, Any]:
    """Read the measured outcomes and propose parameter changes."""
    client = _client()
    try:
        response = await client.messages.create(
            model=MODEL,
            max_tokens=REVIEW_MAX_TOKENS,
            # High effort and a fixed schema: this is the judgement call, and
            # the UI renders the result field by field.
            output_config={
                "effort": "high",
                "format": {"type": "json_schema", "schema": REVIEW_SCHEMA},
            },
            system=REVIEW_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Here is everything the system has measured so far.\n\n"
                        + json.dumps(payload, indent=2, default=str)
                    ),
                }
            ],
        )
    except anthropic.AuthenticationError as error:
        raise AIUnavailable("The saved Anthropic API key was rejected.") from error
    except (anthropic.APIStatusError, anthropic.APIConnectionError) as error:
        raise AIUnavailable(str(error)) from error
    finally:
        await client.close()

    if response.stop_reason == "refusal":
        raise AIUnavailable("the model declined to answer")
    if response.stop_reason == "max_tokens":
        # Structured output truncated mid-JSON is unparseable; say so rather
        # than surfacing a JSONDecodeError from three frames down.
        raise AIUnavailable("the answer was cut short before it was complete")

    try:
        return json.loads(_text(response))
    except ValueError as error:  # pragma: no cover - schema makes this rare
        raise AIUnavailable("the model returned unreadable JSON") from error


async def brief_with_timeout(**kwargs: Any) -> dict[str, Any] | None:
    """:func:`brief_token`, but never worth delaying a signal for."""
    if not enabled():
        return None
    try:
        return await asyncio.wait_for(
            brief_token(**kwargs), timeout=ENRICH_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        log.info("enrichment timed out; sending the signal without a brief")
        return None
    except AIUnavailable as error:
        log.info("enrichment unavailable: %s", error)
        return None
    except Exception:  # pragma: no cover - a brief must never break a signal
        log.exception("enrichment failed")
        return None


__all__ = [
    "AIUnavailable",
    "ENRICH_TIMEOUT_SECONDS",
    "MODEL",
    "brief_token",
    "brief_with_timeout",
    "enabled",
    "parse_brief",
    "review",
]
