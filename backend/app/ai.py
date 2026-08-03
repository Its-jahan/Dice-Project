"""The two places a language model actually earns its keep in DICE.

Where it is *not* used
----------------------
Detection. Which wallets bought what, how many, inside which window, and
whether that crosses a threshold — that is set arithmetic and SQL. Handing it
to a model would make it slower, more expensive, occasionally wrong, and
worst of all **untestable**: the tests that currently pin signal behaviour
would all become approximately true. Contract safety is likewise a dedicated
service's job (:mod:`app.security`), not a model's opinion about bytecode.

Where it does
-------------
**Triage.** A signal says "GEM, 19 of 180 wallets". Finding out what GEM
actually *is* costs a human five minutes, which on a six-hour-old pool is most
of the edge. The enrichment brief answers that question — with web search, so
it can report what the token claims to be rather than paraphrasing numbers
DICE already showed you.

**Review.** Once :mod:`app.performance` has outcomes and :mod:`app.wallets`
has scores, someone has to read them and say "signals under six hours old won;
over 48 lost; move the filter". That is judgement over a small table, which is
exactly what a model is good at — and it only became possible once the outcome
data existed.

Two ways to reach a model
-------------------------
**OpenRouter** (default) is a gateway: one key, an OpenAI-shaped API, and the
same Claude models at the same list price. It is the right choice when the
Anthropic API is not directly reachable, which for a lot of the world it is
not.

**Anthropic direct** uses the official SDK and is used automatically when an
Anthropic key is the one saved.

Both go through :func:`complete`, so the prompts, the parsing, and every
guarantee below are shared. The two backends differ only in how a request is
shaped on the wire — which is exactly as much as should differ.

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
import re
from typing import Any

import httpx

from . import db
from .config import settings

log = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

#: Same model either way — on OpenRouter the slug is namespaced, and it is
#: listed there at Anthropic's own price, so routing through the gateway costs
#: nothing extra on tokens.
DEFAULT_MODELS = {
    "openrouter": "anthropic/claude-opus-5",
    "anthropic": "claude-opus-5",
}

#: Generous, because it is a ceiling rather than a charge — you pay for what
#: is generated. A tight cap only buys the chance of a truncated answer.
ENRICH_MAX_TOKENS = 8_000
REVIEW_MAX_TOKENS = 16_000

#: Enrichment sits in the webhook request path, and Alchemy retries a delivery
#: that answers too slowly. Past this, the signal goes out unenriched — a late
#: brief is worth less than a duplicate delivery.
ENRICH_TIMEOUT_SECONDS = 12.0

#: Web results per brief. OpenRouter bills search per result, so this is the
#: one knob that turns "research every signal" into a line on a bill; five is
#: enough to establish whether a project exists at all.
WEB_RESULTS = 5

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
by hand, so each one costs their attention.

Reply with JSON only, matching the requested schema. No prose around it.\
"""

#: The review returns a fixed shape so the UI can render it without parsing
#: prose. Constraint keywords (minLength, maximum, …) are omitted: neither
#: provider's structured-output compiler supports them, and a schema that is
#: rejected buys nothing.
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


# --------------------------------------------------------------- which provider


def provider() -> str | None:
    """Which backend to use, decided by the key that is actually saved.

    OpenRouter wins when both exist: someone who saved a gateway key chose the
    gateway, and silently preferring the other one would spend on an account
    they were not expecting to use.
    """
    if db.get_setting("openrouter_api_key") or settings.openrouter_api_key:
        return "openrouter"
    if db.get_setting("anthropic_api_key") or settings.anthropic_api_key:
        return "anthropic"
    return None


def api_key(for_provider: str) -> str | None:
    if for_provider == "openrouter":
        return db.get_setting("openrouter_api_key") or settings.openrouter_api_key
    return db.get_setting("anthropic_api_key") or settings.anthropic_api_key


def model() -> str:
    """The configured model, or the right default for the active provider."""
    chosen = (db.get_setting("ai_model") or "").strip()
    if chosen:
        return chosen
    return DEFAULT_MODELS.get(provider() or "openrouter", DEFAULT_MODELS["openrouter"])


def enabled() -> bool:
    """Enrichment runs only when both a key and the toggle are present."""
    stored = db.get_setting("ai_enrichment")
    on = (
        settings.ai_enrichment
        if stored is None
        else stored.strip().lower() in ("1", "true", "yes", "on")
    )
    return bool(on and provider())


# ------------------------------------------------------------------ the call


async def complete(
    *,
    system: str,
    user: str,
    max_tokens: int,
    effort: str,
    schema: dict[str, Any] | None = None,
    web: bool = False,
    timeout: float = 90.0,
) -> str:
    """One completion, from whichever provider is configured. Returns text."""
    active = provider()
    if active is None:
        raise AIUnavailable("No API key saved.")
    key = api_key(active)
    if not key:  # pragma: no cover - provider() already proved one exists
        raise AIUnavailable("No API key saved.")

    if active == "openrouter":
        return await _openrouter(
            key=key, system=system, user=user, max_tokens=max_tokens,
            effort=effort, schema=schema, web=web, timeout=timeout,
        )
    return await _anthropic(
        key=key, system=system, user=user, max_tokens=max_tokens,
        effort=effort, schema=schema, web=web, timeout=timeout,
    )


async def _openrouter(
    *, key: str, system: str, user: str, max_tokens: int, effort: str,
    schema: dict[str, Any] | None, web: bool, timeout: float,
) -> str:
    """OpenAI-shaped request against the gateway."""
    payload: dict[str, Any] = {
        "model": model(),
        "max_tokens": max_tokens,
        "reasoning": {"effort": effort},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if web:
        # The gateway runs the search itself and folds the results into the
        # prompt, so the model needs no tool loop of its own.
        payload["plugins"] = [{"id": "web", "max_results": WEB_RESULTS}]
    if schema is not None:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "review", "strict": True, "schema": schema},
        }

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                OPENROUTER_URL,
                json=payload,
                headers={
                    "Authorization": f"Bearer {key}",
                    # Identifies this app on OpenRouter's side; harmless and
                    # makes the spend legible on their dashboard.
                    "HTTP-Referer": "https://github.com/Its-jahan/Dice-Project",
                    "X-Title": "DICE",
                },
            )
    except httpx.HTTPError as error:
        raise AIUnavailable(f"OpenRouter did not answer: {error}") from error

    if response.status_code >= 400:
        raise AIUnavailable(f"OpenRouter returned {response.status_code}: "
                            f"{_openrouter_error(response)}")
    try:
        body = response.json()
    except ValueError as error:
        raise AIUnavailable("OpenRouter returned a non-JSON body") from error

    # A 200 can still carry an error — an upstream provider failure is
    # reported in-band rather than as a status code.
    if isinstance(body.get("error"), dict):
        raise AIUnavailable(str(body["error"].get("message") or body["error"]))

    choices = body.get("choices") or []
    if not choices:
        raise AIUnavailable("OpenRouter returned no completion")
    message = choices[0].get("message") or {}
    if message.get("refusal"):
        raise AIUnavailable("the model declined to answer")
    text = (message.get("content") or "").strip()
    if not text:
        # An empty content with a length finish reason is a truncation, which
        # is worth saying plainly rather than reporting as "returned nothing".
        if choices[0].get("finish_reason") == "length":
            raise AIUnavailable("the answer was cut short before it was complete")
        raise AIUnavailable("the model returned nothing")
    return text


def _openrouter_error(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text.strip()[:200] or response.reason_phrase
    error = body.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error)
    return str(error or body)[:200]


async def _anthropic(
    *, key: str, system: str, user: str, max_tokens: int, effort: str,
    schema: dict[str, Any] | None, web: bool, timeout: float,
) -> str:
    """The official SDK, for a key that talks to Anthropic directly."""
    try:
        import anthropic
    except ImportError as error:  # pragma: no cover - dependency is pinned
        raise AIUnavailable("The anthropic package is not installed.") from error

    output_config: dict[str, Any] = {"effort": effort}
    if schema is not None:
        output_config["format"] = {"type": "json_schema", "schema": schema}
    request: dict[str, Any] = {
        "model": model(),
        "max_tokens": max_tokens,
        "output_config": output_config,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    if web:
        request["tools"] = [
            {"type": "web_search_20260209", "name": "web_search",
             "max_uses": WEB_RESULTS}
        ]

    client = anthropic.AsyncAnthropic(api_key=key, timeout=timeout)
    try:
        response = await client.messages.create(**request)
        if response.stop_reason == "pause_turn":
            # A server-side search can exhaust its tool loop and come back a
            # complete-looking half answer. Resume once; a second pause means
            # the search is wandering and the partial beats more latency.
            response = await client.messages.create(
                **request,
                messages=[
                    *request["messages"],
                    {"role": "assistant", "content": response.content},
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
        raise AIUnavailable("the answer was cut short before it was complete")
    text = "\n".join(
        block.text for block in response.content if getattr(block, "type", "") == "text"
    ).strip()
    if not text:
        raise AIUnavailable("the model returned nothing")
    return text


# ----------------------------------------------------------------- the briefs


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
    *, chain: str, token_address: str, symbol: str | None, facts: dict[str, Any]
) -> dict[str, Any]:
    """Research a signalled token and return a short brief.

    Raises :class:`AIUnavailable` on any failure — callers treat a missing
    brief as normal, because a signal without one is still a signal.
    """
    lines = [f"Chain: {chain}", f"Token: {symbol or 'unknown symbol'} ({token_address})"]
    for label, value in facts.items():
        if value not in (None, "", [], {}):
            lines.append(f"{label}: {value}")

    text = await complete(
        system=ENRICH_SYSTEM,
        user="\n".join(lines),
        max_tokens=ENRICH_MAX_TOKENS,
        # Low effort: search-and-summarise with a fixed output shape, and the
        # latency sits in a webhook request path.
        effort="low",
        web=True,
        timeout=ENRICH_TIMEOUT_SECONDS + 5,
    )
    return parse_brief(text)


def parse_json(text: str) -> dict[str, Any]:
    """Read the review JSON, tolerating a model that wrapped it in prose.

    Structured output is requested, but not every model on a gateway honours
    it. Falling back to the first JSON object in the text turns "the model
    added a sentence" from a failure into a non-event.
    """
    try:
        return json.loads(text)
    except ValueError:
        pass
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        braces = re.search(r"\{.*\}", text, re.S)
        candidate = braces.group(0) if braces else None
    if candidate is None:
        raise AIUnavailable("the model returned unreadable JSON")
    try:
        return json.loads(candidate)
    except ValueError as error:
        raise AIUnavailable("the model returned unreadable JSON") from error


async def review(payload: dict[str, Any]) -> dict[str, Any]:
    """Read the measured outcomes and propose parameter changes."""
    text = await complete(
        system=REVIEW_SYSTEM,
        user=(
            "Here is everything the system has measured so far.\n\n"
            + json.dumps(payload, indent=2, default=str)
        ),
        max_tokens=REVIEW_MAX_TOKENS,
        # High effort and a fixed schema: this is the judgement call, and the
        # UI renders the result field by field.
        effort="high",
        schema=REVIEW_SCHEMA,
    )
    return parse_json(text)


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


async def check_key() -> dict[str, Any]:
    """Prove the saved key works, without waiting for a signal to find out."""
    text = await complete(
        system="Reply with the single word: ok",
        user="Reply with the single word: ok",
        max_tokens=1_000,
        effort="low",
        timeout=45.0,
    )
    return {"ok": True, "provider": provider(), "model": model(), "reply": text[:80]}


__all__ = [
    "AIUnavailable",
    "DEFAULT_MODELS",
    "ENRICH_TIMEOUT_SECONDS",
    "api_key",
    "brief_token",
    "brief_with_timeout",
    "check_key",
    "complete",
    "enabled",
    "model",
    "parse_brief",
    "parse_json",
    "provider",
    "review",
]
