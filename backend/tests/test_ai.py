"""The AI layer, and the guarantee that it can never cost a signal.

Every test here that matters is a negative one: no key, a timeout, a refusal,
a broken API. The system's behaviour in all four cases must be identical to
having no AI layer at all — the signal fires, the Telegram goes out, only the
extra paragraph is missing.
"""

import asyncio
import hashlib
import hmac
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import ai, db, dexscreener, main
from app.cache import DiskCache
from app.config import settings
from app.jobs import JobStore

SIGNING_KEY = "whsec-ai"
WEBHOOK_ID = "wh_ai"
WALLETS = [f"0x{i:040x}" for i in range(1, 11)]
GEM = "0x" + "9" * 40

BRIEF = """\
THEME: gaming
WHAT: A token for an unreleased browser game; the site went up four days ago.
READ: The buying is early but the project has no shipped product yet.
RISK: The team is anonymous and liquidity is not locked.\
"""


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "dice.db"))
    monkeypatch.setattr(settings, "monitor_enabled", False)
    monkeypatch.setattr(settings, "live_sweep_seconds", 3600)
    monkeypatch.setattr(settings, "pool_pct", 50.0)
    monkeypatch.setattr(settings, "pool_min_wallets", 3)
    monkeypatch.setattr(settings, "dune_api_key", None, raising=False)
    monkeypatch.setattr(settings, "telegram_bot_token", None, raising=False)
    monkeypatch.setattr(settings, "public_base_url", None, raising=False)
    monkeypatch.setattr(settings, "anthropic_api_key", None, raising=False)
    monkeypatch.setattr(main, "cache", DiskCache(directory=tmp_path / "cache"))
    monkeypatch.setattr(main, "store", JobStore(directory=tmp_path / "jobs"))

    async def market_data(chain, addresses, refresh=False):
        return {
            str(a).lower(): {
                "has_pair": True, "price_usd": 0.01, "liquidity_usd": 50_000.0,
                "volume_24h": 9_000.0, "fdv": None, "symbol": "GEM",
                "name": None, "pair_url": None, "pair_created_at": None,
            }
            for a in addresses
        }

    monkeypatch.setattr(dexscreener, "market_data", market_data)
    with TestClient(main.app) as test_client:
        yield test_client


@pytest.fixture
def store(monkeypatch, tmp_path):
    """A bare database — the gateway tests need no app, only settings."""
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "dice.db"))
    monkeypatch.setattr(settings, "openrouter_api_key", None, raising=False)
    monkeypatch.setattr(settings, "anthropic_api_key", None, raising=False)
    with db.connect():
        pass
    return db


def _live_watchlist():
    created = db.create_watchlist(
        name="early buyers", chain="ethereum", wallets=WALLETS,
        source_token_address=None, notes="", min_wallets=5, min_wallets_pct=0.0,
        buy_window_hours=48, monitor_interval_hours=24.0, min_buy_usd=0.0,
        auto_monitor=False, ignore_tokens=[], realtime=True,
    )
    db.save_webhook(
        chain="ethereum", network="ETH_MAINNET", webhook_id=WEBHOOK_ID,
        signing_key=SIGNING_KEY,
        webhook_url="https://dice.example/api/webhooks/alchemy",
        address_count=len(WALLETS),
    )
    return created


def _buy(client, wallet):
    tx = f"0xb{wallet[-6:]}"
    payload = {
        "webhookId": WEBHOOK_ID,
        "type": "ADDRESS_ACTIVITY",
        "event": {"network": "ETH_MAINNET", "activity": [
            {"fromAddress": "0x" + "1" * 40, "toAddress": wallet, "hash": tx,
             "blockNum": "0x1", "value": 100.0, "asset": "GEM",
             "category": "erc20", "rawContract": {"address": GEM}},
            {"fromAddress": wallet, "toAddress": "0x" + "d" * 40, "hash": tx,
             "blockNum": "0x1", "value": 0.4, "asset": "ETH",
             "category": "external", "rawContract": {"address": None}},
        ]},
    }
    body = json.dumps(payload).encode()
    signature = hmac.new(SIGNING_KEY.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        "/api/webhooks/alchemy", content=body,
        headers={"X-Alchemy-Signature": signature,
                 "Content-Type": "application/json"},
    )


# ------------------------------------------------------------------- parsing


def test_the_brief_is_split_into_its_labelled_parts():
    parsed = ai.parse_brief(BRIEF)
    assert parsed["theme"] == "gaming"
    assert parsed["what"].startswith("A token for an unreleased browser game")
    assert parsed["risk"].startswith("The team is anonymous")
    # The raw text is always kept — the labels are for display only.
    assert parsed["text"] == BRIEF


def test_prose_instead_of_the_shape_still_yields_a_usable_brief():
    """A model that ignores the format must not produce an empty brief."""
    parsed = ai.parse_brief("This token appears to be a fork of something.")
    assert parsed["theme"] is None
    assert parsed["what"] == ""
    assert parsed["text"] == "This token appears to be a fork of something."


def test_the_theme_is_reduced_to_one_grouping_key():
    assert ai.parse_brief("THEME: Gaming.\nWHAT: x")["theme"] == "gaming"
    assert ai.parse_brief("THEME: ai infrastructure\nWHAT: x")["theme"] == "ai"


# ------------------------------------------------------- never costs a signal


@pytest.mark.anyio
async def test_without_a_key_nothing_is_attempted(client, monkeypatch):
    called = False

    async def brief_token(**kwargs):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(ai, "brief_token", brief_token)
    _live_watchlist()
    for wallet in WALLETS[:6]:
        _buy(client, wallet)

    assert len(client.get("/api/signals").json()) == 1
    assert called is False  # no key, so no request and no spend


@pytest.mark.anyio
async def test_a_slow_model_does_not_delay_the_signal(client, monkeypatch):
    """The brief sits in the webhook path; Alchemy retries a slow delivery."""
    monkeypatch.setattr(ai, "ENRICH_TIMEOUT_SECONDS", 0.05)
    db.set_setting("anthropic_api_key", "sk-ant-test")

    async def hangs(**kwargs):
        await asyncio.sleep(30)

    monkeypatch.setattr(ai, "brief_token", hangs)
    _live_watchlist()
    for wallet in WALLETS[:6]:
        _buy(client, wallet)

    signals = client.get("/api/signals").json()
    assert len(signals) == 1
    # Fired on time, with no brief rather than a late one.
    assert db.get_brief(signals[0]["id"]) is None


@pytest.mark.anyio
async def test_a_refusal_or_outage_is_not_an_error(client, monkeypatch):
    db.set_setting("anthropic_api_key", "sk-ant-test")

    async def refuses(**kwargs):
        raise ai.AIUnavailable("the model declined to answer")

    monkeypatch.setattr(ai, "brief_token", refuses)
    _live_watchlist()
    for wallet in WALLETS[:6]:
        _buy(client, wallet)

    assert len(client.get("/api/signals").json()) == 1


@pytest.mark.anyio
async def test_an_unexpected_exception_still_lets_the_signal_through(
    client, monkeypatch
):
    db.set_setting("anthropic_api_key", "sk-ant-test")

    async def explodes(**kwargs):
        raise ValueError("something nobody predicted")

    monkeypatch.setattr(ai, "brief_token", explodes)
    _live_watchlist()
    for wallet in WALLETS[:6]:
        _buy(client, wallet)

    assert len(client.get("/api/signals").json()) == 1


# ------------------------------------------------------------------ storage


@pytest.mark.anyio
async def test_a_brief_is_stored_and_reaches_the_signal(client, monkeypatch):
    db.set_setting("anthropic_api_key", "sk-ant-test")
    seen = {}

    async def brief_token(**kwargs):
        seen.update(kwargs)
        return ai.parse_brief(BRIEF)

    monkeypatch.setattr(ai, "brief_token", brief_token)
    _live_watchlist()
    for wallet in WALLETS[:6]:
        _buy(client, wallet)

    signal_id = client.get("/api/signals").json()[0]["id"]
    stored = client.get(f"/api/signals/{signal_id}/brief").json()
    assert stored["theme"] == "gaming"
    assert "browser game" in stored["what"]

    # The model is only ever given facts DICE already established, and it is
    # asked at the moment the threshold is crossed — the fifth wallet, not
    # whatever the count has grown to by the time anyone reads the signal.
    assert seen["token_address"] == GEM
    assert "5 of 10 tracked" in seen["facts"]["Wallets that bought it"]


@pytest.mark.anyio
async def test_a_strengthened_signal_is_not_re_briefed(client, monkeypatch):
    """Same opportunity, already answered — re-briefing would just cost money."""
    db.set_setting("anthropic_api_key", "sk-ant-test")
    calls = 0

    async def brief_token(**kwargs):
        nonlocal calls
        calls += 1
        return ai.parse_brief(BRIEF)

    monkeypatch.setattr(ai, "brief_token", brief_token)
    _live_watchlist()
    for wallet in WALLETS[:6]:
        _buy(client, wallet)
    for wallet in WALLETS[6:9]:
        _buy(client, wallet)  # strengthens the same signal

    assert calls == 1


def test_a_signal_without_a_brief_says_so(client):
    _live_watchlist()
    for wallet in WALLETS[:6]:
        _buy(client, wallet)
    signal_id = client.get("/api/signals").json()[0]["id"]
    assert client.get(f"/api/signals/{signal_id}/brief").status_code == 404


# ------------------------------------------------------------------- themes


def test_themes_are_grouped_with_their_measured_returns(client):
    _live_watchlist()
    for wallet in WALLETS[:6]:
        _buy(client, wallet)
    signal_id = client.get("/api/signals").json()[0]["id"]

    db.save_brief(
        signal_id=signal_id, chain="ethereum", token_address=GEM,
        brief=ai.parse_brief(BRIEF), model="claude-opus-5",
    )
    # The signal already stamped its own entry price when it fired (0.01);
    # recording another here would be ignored, so score against the real one.
    db.set_outcome_price(signal_id, "price_24h", 0.025)

    themes = db.theme_counts()
    assert themes == [{"theme": "gaming", "signals": 1, "avg_return_24h": 150.0}]


# ------------------------------------------------------------------- review


def test_the_review_needs_a_key(client):
    response = client.post("/api/ai/review")
    assert response.status_code == 503
    assert "key" in response.json()["detail"].lower()


def test_the_review_is_stored_so_it_need_not_be_paid_for_twice(
    client, monkeypatch
):
    db.set_setting("anthropic_api_key", "sk-ant-test")
    verdict = {
        "verdict": "Three scored signals is not enough to conclude anything.",
        "confidence": "none",
        "recommendations": [],
        "watch_next": "More scored signals — twenty would support a threshold change.",
    }

    async def review(payload):
        # The model is handed measurements, not raw rows to re-derive.
        assert "scoreboard" in payload and "current_settings" in payload
        return verdict

    monkeypatch.setattr(ai, "review", review)
    assert client.post("/api/ai/review").json()["confidence"] == "none"

    stored = client.get("/api/ai/review").json()["review"]
    assert stored["verdict"] == verdict["verdict"]
    assert stored["at"]


def test_review_failures_surface_as_unavailable_not_as_a_crash(
    client, monkeypatch
):
    db.set_setting("anthropic_api_key", "sk-ant-test")

    async def review(payload):
        raise ai.AIUnavailable("the answer was cut short before it was complete")

    monkeypatch.setattr(ai, "review", review)
    response = client.post("/api/ai/review")
    assert response.status_code == 503
    assert "cut short" in response.json()["detail"]


# ------------------------------------------------------------------ settings


def test_the_key_is_saved_on_the_server_and_never_read_back(client):
    body = client.put(
        "/api/settings/ai", json={"anthropic_api_key": "sk-ant-secret-value"}
    ).json()
    assert body["configured"] is True
    # Only a hint, never the key itself.
    assert "secret" not in json.dumps(body)

    cleared = client.put("/api/settings/ai", json={"anthropic_api_key": ""}).json()
    assert cleared["configured"] is False


def test_enrichment_can_be_switched_off_without_removing_the_key(client):
    client.put("/api/settings/ai", json={"anthropic_api_key": "sk-ant-test"})
    off = client.put("/api/settings/ai", json={"enrichment": False}).json()
    assert off["configured"] is True and off["enrichment"] is False
    assert ai.enabled() is False


# --------------------------------------------------------------- openrouter
#
# The gateway is the path this deployment actually uses, and its wire format
# is the one thing here that cannot be checked without a live key. These pin
# the request shape against a stubbed transport so a typo in the payload
# surfaces at test time rather than the first time a signal fires.


#: Captured once at import. Reading httpx.AsyncClient inside the helper would
#: pick up a previous stub when a test installs two in a row, and the first
#: handler would silently answer the second request.
REAL_ASYNC_CLIENT = httpx.AsyncClient


def _openrouter_stub(monkeypatch, handler):
    """Point the OpenRouter client at a handler and capture what it sent."""
    sent = {}

    def transport_handler(request: httpx.Request) -> httpx.Response:
        sent["url"] = str(request.url)
        sent["auth"] = request.headers.get("Authorization")
        sent["body"] = json.loads(request.content)
        return handler(sent["body"])

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(transport_handler)
        return REAL_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr(ai.httpx, "AsyncClient", patched)
    return sent


def _reply(text, **extra):
    return lambda body: httpx.Response(
        200,
        json={"choices": [{"message": {"content": text}, **extra}]},
    )


@pytest.mark.anyio
async def test_openrouter_is_preferred_when_both_keys_are_saved(store):
    """Saving a gateway key is a choice; spending on the other account is not."""
    db.set_setting("anthropic_api_key", "sk-ant-x")
    assert ai.provider() == "anthropic"
    db.set_setting("openrouter_api_key", "sk-or-x")
    assert ai.provider() == "openrouter"
    assert ai.model() == "anthropic/claude-opus-5"


@pytest.mark.anyio
async def test_a_brief_request_asks_the_gateway_to_search(store, monkeypatch):
    db.set_setting("openrouter_api_key", "sk-or-test")
    sent = _openrouter_stub(monkeypatch, _reply(BRIEF))

    brief = await ai.brief_token(
        chain="ethereum", token_address=GEM, symbol="GEM",
        facts={"Wallets that bought it": "19 of 180 tracked"},
    )

    assert brief["theme"] == "gaming"
    assert sent["url"] == ai.OPENROUTER_URL
    assert sent["auth"] == "Bearer sk-or-test"
    assert sent["body"]["model"] == "anthropic/claude-opus-5"
    # Search is the whole point of the brief — without it the model can only
    # restate numbers DICE already showed the operator.
    assert sent["body"]["plugins"] == [
        {"id": "web", "max_results": ai.WEB_RESULTS}
    ]
    assert sent["body"]["reasoning"] == {"effort": "low"}
    # Only facts DICE established are sent.
    assert "19 of 180 tracked" in sent["body"]["messages"][1]["content"]


@pytest.mark.anyio
async def test_a_review_request_asks_for_the_schema_and_does_not_search(
    store, monkeypatch
):
    db.set_setting("openrouter_api_key", "sk-or-test")
    verdict = {
        "verdict": "Too few signals.", "confidence": "none",
        "recommendations": [], "watch_next": "More scored signals.",
    }
    sent = _openrouter_stub(monkeypatch, _reply(json.dumps(verdict)))

    assert (await ai.review({"scoreboard": {}}))["confidence"] == "none"
    fmt = sent["body"]["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["schema"]["required"] == [
        "verdict", "confidence", "recommendations", "watch_next"
    ]
    assert sent["body"]["reasoning"] == {"effort": "high"}
    # No web search: the review reads measured numbers, not the internet.
    assert "plugins" not in sent["body"]


@pytest.mark.anyio
async def test_a_gateway_error_becomes_unavailable_not_a_crash(store, monkeypatch):
    db.set_setting("openrouter_api_key", "sk-or-test")
    _openrouter_stub(
        monkeypatch,
        lambda body: httpx.Response(
            402, json={"error": {"message": "Insufficient credits"}}
        ),
    )
    with pytest.raises(ai.AIUnavailable, match="Insufficient credits"):
        await ai.review({})


@pytest.mark.anyio
async def test_an_error_inside_a_200_is_still_an_error(store, monkeypatch):
    """The gateway reports an upstream failure in-band, with a 200 status."""
    db.set_setting("openrouter_api_key", "sk-or-test")
    _openrouter_stub(
        monkeypatch,
        lambda body: httpx.Response(
            200, json={"error": {"message": "upstream provider is down"}}
        ),
    )
    with pytest.raises(ai.AIUnavailable, match="upstream provider is down"):
        await ai.review({})


@pytest.mark.anyio
async def test_a_refusal_and_a_truncation_are_told_apart(store, monkeypatch):
    db.set_setting("openrouter_api_key", "sk-or-test")

    _openrouter_stub(
        monkeypatch,
        lambda body: httpx.Response(
            200, json={"choices": [{"message": {"refusal": "no", "content": None}}]}
        ),
    )
    with pytest.raises(ai.AIUnavailable, match="declined"):
        await ai.review({})

    _openrouter_stub(
        monkeypatch,
        lambda body: httpx.Response(
            200,
            json={"choices": [{"message": {"content": ""}, "finish_reason": "length"}]},
        ),
    )
    with pytest.raises(ai.AIUnavailable, match="cut short"):
        await ai.review({})


@pytest.mark.anyio
async def test_json_wrapped_in_prose_or_fences_is_still_read(store, monkeypatch):
    """Not every model on a gateway honours a response format."""
    db.set_setting("openrouter_api_key", "sk-or-test")
    verdict = {"verdict": "v", "confidence": "low", "recommendations": [],
               "watch_next": "w"}

    _openrouter_stub(
        monkeypatch,
        _reply("Here is the review:\n```json\n" + json.dumps(verdict) + "\n```"),
    )
    assert (await ai.review({}))["confidence"] == "low"

    _openrouter_stub(monkeypatch, _reply("Sure! " + json.dumps(verdict)))
    assert (await ai.review({}))["confidence"] == "low"


def test_unreadable_output_says_so_rather_than_raising_a_json_error():
    with pytest.raises(ai.AIUnavailable, match="unreadable JSON"):
        ai.parse_json("I could not complete this request.")


@pytest.mark.anyio
async def test_the_key_can_be_proved_before_a_signal_needs_it(store, monkeypatch):
    db.set_setting("openrouter_api_key", "sk-or-test")
    _openrouter_stub(monkeypatch, _reply("ok"))
    result = await ai.check_key()
    assert result == {
        "ok": True, "provider": "openrouter",
        "model": "anthropic/claude-opus-5", "reply": "ok",
    }


# ---------------------------------------------------------------- catalogue


def _catalogue_stub(monkeypatch, payload, status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return REAL_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr(ai.httpx, "AsyncClient", patched)


@pytest.fixture(autouse=True)
def _clear_catalogue():
    """The catalogue is cached process-wide; tests must not leak into each other."""
    ai._catalogue.update({"at": 0.0, "provider": None, "models": []})
    yield
    ai._catalogue.update({"at": 0.0, "provider": None, "models": []})


@pytest.mark.anyio
async def test_the_catalogue_is_normalised_and_ordered(store, monkeypatch):
    _catalogue_stub(monkeypatch, {"data": [
        {"id": "z/no-schema", "name": "Z", "context_length": 8000,
         "pricing": {"prompt": "0.000001", "completion": "0.000002"},
         "supported_parameters": ["max_tokens"]},
        {"id": "a/schema", "name": "A", "context_length": 200000,
         "pricing": {"prompt": "0.000005", "completion": "0.000025"},
         "supported_parameters": ["structured_outputs", "response_format"]},
    ]})

    models = await ai.list_models()

    # Schema-capable first: the review asks for a fixed shape, and a model
    # that honours it is more reliable than the prose fallback.
    assert [m["id"] for m in models] == ["a/schema", "z/no-schema"]
    assert models[0]["input_per_m"] == 5.0
    assert models[0]["output_per_m"] == 25.0
    assert models[0]["structured_outputs"] is True
    assert models[1]["structured_outputs"] is False


@pytest.mark.anyio
async def test_variable_pricing_is_reported_as_unknown(store, monkeypatch):
    """A router that picks per request quotes -1; that is not a price."""
    _catalogue_stub(monkeypatch, {"data": [
        {"id": "openrouter/auto", "name": "Auto",
         "pricing": {"prompt": "-1", "completion": "-1"},
         "supported_parameters": ["structured_outputs"]},
    ]})
    model = (await ai.list_models())[0]
    assert model["input_per_m"] is None and model["output_per_m"] is None


@pytest.mark.anyio
async def test_the_catalogue_is_cached_between_calls(store, monkeypatch):
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"data": [
            {"id": "a/b", "pricing": {"prompt": "0", "completion": "0"},
             "supported_parameters": []},
        ]})

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return REAL_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr(ai.httpx, "AsyncClient", patched)

    await ai.list_models()
    await ai.list_models()
    assert calls == 1
    await ai.list_models(refresh=True)
    assert calls == 2


@pytest.mark.anyio
async def test_a_catalogue_outage_does_not_look_like_an_empty_catalogue(
    store, monkeypatch
):
    """An empty picker and a broken one need to be told apart."""
    _catalogue_stub(monkeypatch, {"error": "nope"}, status=500)
    with pytest.raises(ai.AIUnavailable, match="Could not read the model list"):
        await ai.list_models()


@pytest.mark.anyio
async def test_a_chosen_model_is_used_for_the_request(store, monkeypatch):
    db.set_setting("openrouter_api_key", "sk-or-test")
    db.set_setting("ai_model", "google/gemini-3-pro")
    sent = _openrouter_stub(monkeypatch, _reply(BRIEF))

    await ai.brief_token(chain="ethereum", token_address=GEM, symbol="GEM", facts={})

    assert sent["body"]["model"] == "google/gemini-3-pro"
    assert ai.model() == "google/gemini-3-pro"
