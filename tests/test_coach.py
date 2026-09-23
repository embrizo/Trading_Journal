"""Coach unit tests with fake Anthropic and Gemini clients — no network, no key."""
import asyncio
import inspect
import json
from types import SimpleNamespace

import pytest

from break_signal.config import AiCfg
from break_signal.journal import coach as C
from break_signal.journal import prompts
from break_signal.journal.db import JournalDB
from break_signal.journal.tools import Tools


# ── fakes ────────────────────────────────────────────────────────────────────
class FakeRunner:
    """Pretends to be the SDK tool runner: calls the named tools, then answers."""

    def __init__(self, tools, script, answer):
        self.tools = {t.name: t for t in tools}
        self.script = script          # [(tool_name, input_dict), ...]
        self.answer = answer

    async def until_done(self):
        for name, inp in self.script:
            await self.tools[name].call(inp)   # exercises the real wrapper → Tools → analytics
        return SimpleNamespace(
            content=[SimpleNamespace(type="thinking", thinking=""),
                     SimpleNamespace(type="text", text=self.answer)],
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=100, output_tokens=50, cache_read_input_tokens=0,
                                  cache_creation_input_tokens=90),
        )


class FakeClient:
    def __init__(self, script=(), answer="", review=None):
        self.script, self.answer, self.review = list(script), answer, review
        self.calls = []
        client = self

        class Messages:
            def tool_runner(self, **kw):
                client.calls.append(kw)
                return FakeRunner(kw["tools"], client.script, client.answer)

            async def parse(self, **kw):
                client.calls.append(kw)
                return SimpleNamespace(parsed_output=kw["output_format"](**client.review))

        self.beta = SimpleNamespace(messages=Messages())
        self.messages = Messages()


class FakeGeminiChat:
    def __init__(self, client, config, model):
        self.client, self.config, self.model = client, config, model

    async def send_message(self, message):
        self.client.sent.append(message)
        self.client._maybe_raise(self.model)
        by_name = {f.__name__: f for f in self.config["tools"]}
        for name, kw in self.client.script:
            await by_name[name](**kw)      # what automatic function calling does
        return SimpleNamespace(text=self.client.answer, usage_metadata=SimpleNamespace(
            prompt_token_count=100, candidates_token_count=50, total_token_count=150))


class FakeGemini:
    """Pretends to be google.genai.Client (only the aio surface the coach uses).

    ``errors``, if given, maps a model name to an exception raised the first
    time that model is called (simulating a 429 on the primary model before a
    fallback retry succeeds on a different one).
    """

    def __init__(self, script=(), answer="", text="", errors=None):
        self.script, self.answer, self.text = list(script), answer, text
        self.errors = dict(errors or {})
        self.sent, self.calls = [], []
        client = self

        class Chats:
            def create(self, **kw):
                client.calls.append(kw)
                return FakeGeminiChat(client, kw["config"], kw["model"])

        class Models:
            async def generate_content(self, **kw):
                client.calls.append(kw)
                client._maybe_raise(kw["model"])
                return SimpleNamespace(text=client.text, usage_metadata=None)

        self.aio = SimpleNamespace(chats=Chats(), models=Models())

    def _maybe_raise(self, model: str) -> None:
        exc = self.errors.pop(model, None)     # raised once per model, then clears
        if exc is not None:
            raise exc


def _gemini(**kw):
    return AiCfg(provider="gemini", model="gemini-3.6-flash", **kw)


@pytest.fixture
def tools():
    t = Tools(JournalDB(":memory:"))
    for d, x, tags in [("LONG", 120, ["Breakout"]), ("LONG", 90, ["FOMO"]), ("LONG", 115, ["Retest"])]:
        tid = t.add_trade("SOL", d, tf="4H", entry_price=100, sl_price=90, tags=tags, auto_link=False)["trade"]["id"]
        t.close_trade(tid, x)
    yield t
    t.db.close()


# ── parity guard ─────────────────────────────────────────────────────────────
def test_parity_check_accepts_numbers_from_results():
    tc = C.ToolCall("journal_stats", {"period": "all"},
                    {"n": 7, "win_rate": 0.571, "avg_r": 0.8, "trades": [{"id": 12}]})
    text = "You have 7 trades (n=7), 57% win, avg +0.8R; see #12. Rate 0.57."
    assert C.parity_check(text, [tc]) == []


def test_parity_check_flags_invented_numbers():
    tc = C.ToolCall("journal_stats", {}, {"n": 7, "win_rate": 0.571})
    missing = C.parity_check("7 trades, 57% win, expectancy 1.9R and a 12% edge", [tc])
    assert missing == ["1.9", "12"]


def test_chunk_prefers_paragraph_breaks():
    text = "a" * 3000 + "\n" + "b" * 3000
    parts = C.chunk(text, 4000)
    assert parts == ["a" * 3000, "b" * 3000]
    assert C.chunk("x" * 9000, 4000) == ["x" * 4000, "x" * 4000, "x" * 1000]


# ── tool surface ─────────────────────────────────────────────────────────────
def test_coach_tools_are_read_only_and_have_schemas(tools):
    coach = C.Coach(tools, AiCfg(), client=FakeClient())
    names = [t.name for t in coach.build_tools()]
    assert "market_snapshot" in names and "journal_stats" in names and "journal_rule_check" in names
    for bad in ("add", "close", "skip", "update", "delete", "tag_trade", "set_rule"):
        assert not any(bad in n for n in names), names
    for t in coach.build_tools():
        d = t.to_dict()
        assert d["description"] and d["input_schema"]["type"] == "object"


def test_wrapper_returns_json_and_records_call(tools):
    coach = C.Coach(tools, AiCfg(), client=FakeClient())
    stats = next(t for t in coach.build_tools() if t.name == "journal_stats")
    out = json.loads(asyncio.run(stats.call({"period": "all", "tf": "4H"})))
    assert out["n"] == 3 and out["wins"] == 2
    assert coach._calls[0].name == "journal_stats" and coach._calls[0].args["tf"] == "4H"


# ── /ask ─────────────────────────────────────────────────────────────────────
def test_ask_runs_tools_stores_and_parity_checks(tools):
    fake = FakeClient(
        script=[("journal_stats", {"period": "all"}), ("journal_tag_stats", {"phase": "ENTRY"})],
        answer="YOUR HISTORY: 3 trades (n=3, small sample), 67% win, avg +0.83R. FOMO: 0/1. YOUR DECISION.",
    )
    coach = C.Coach(tools, AiCfg(max_tool_calls=5), client=fake)
    ans = asyncio.run(coach.ask("how am I doing on 4H breaks?"))
    assert [c.name for c in ans.tool_calls] == ["journal_stats", "journal_tag_stats"]
    assert ans.tool_calls[0].result["n"] == 3
    assert ans.unverified_numbers == []
    assert ans.usage["output_tokens"] == 50 and ans.stop_reason == "end_turn"
    # request shape
    kw = fake.calls[0]
    assert kw["model"] == "claude-opus-5" and kw["max_iterations"] == 5
    assert kw["thinking"] == {"type": "adaptive"}
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert kw["messages"][-1]["role"] == "user"
    # stored for audit
    row = tools.db.conn.execute("SELECT kind, model, prompt_version, input_metrics, output FROM ai_analysis").fetchone()
    assert row["kind"] == "suggestion" and row["prompt_version"] == "coach_v1"
    stored = json.loads(row["input_metrics"])
    assert stored["tool_calls"][0]["name"] == "journal_stats" and stored["question"].startswith("how am I")
    assert ans.analysis_id is not None


def test_ask_flags_invented_numbers(tools):
    fake = FakeClient(script=[("journal_stats", {})], answer="Your win rate is 80% (n=3) with 9.9R expectancy.")
    ans = asyncio.run(C.Coach(tools, AiCfg(), client=fake).ask("?"))
    assert ans.unverified_numbers == ["80", "9.9"]


def test_ask_signal_appended_to_user_turn(tools):
    fake = FakeClient(answer="ok")
    asyncio.run(C.Coach(tools, AiCfg(), client=fake).ask("take it?", signal={"event": "break_up", "rsi": 68}))
    content = fake.calls[0]["messages"][0]["content"]
    assert content.startswith("take it?") and '"rsi": 68' in content


def test_daily_budget(tools):
    fake = FakeClient(answer="ok")
    coach = C.Coach(tools, AiCfg(daily_ask_limit=2), client=fake)
    asyncio.run(coach.ask("1")); asyncio.run(coach.ask("2"))
    assert coach.asks_today() == 2
    with pytest.raises(C.AskBudgetExceeded):
        asyncio.run(coach.ask("3"))
    # no_store answers don't count
    asyncio.run(C.Coach(tools, AiCfg(daily_ask_limit=99), client=fake).ask("x", store=False))
    assert coach.asks_today() == 2


def test_missing_credentials_is_a_clean_error(tools, monkeypatch):
    """Real SDK client, no key → CoachError with a hint, never a raw TypeError traceback.
    ANTHROPIC_BASE_URL points at a closed port so a dev box with an `ant auth` profile
    fails fast on connection instead of spending money."""
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:9")
    coach = C.Coach(tools, AiCfg())
    with pytest.raises(C.CoachError) as ei:
        asyncio.run(coach.ask("hi", store=False))
    assert "ANTHROPIC_API_KEY" in str(ei.value) or "cannot reach" in str(ei.value)


# ── /review ──────────────────────────────────────────────────────────────────
_REVIEW = {"facts": ["LONG SOL 4H, exit 120 (#1)"], "metrics": ["R 2.0", "same setup n=2"],
           "rule_violations": [], "observations": ["held to target"], "chart_observations": [],
           "questions": ["was there a retest?"]}


def test_review_structured_and_stored(tools):
    fake = FakeClient(review=_REVIEW)
    coach = C.Coach(tools, AiCfg(), client=fake)
    r = asyncio.run(coach.review(1))
    assert r["trade_id"] == 1 and r["facts"] and r["prompt_version"] == "review_v1"
    assert r["unverified_numbers"] == [] and r["images_used"] == [] and r["images_skipped"] == []
    kw = fake.calls[0]
    assert kw["system"][0]["text"].startswith("You are reviewing ONE closed trade")
    assert "No chart screenshots" in kw["system"][0]["text"]
    content = kw["messages"][0]["content"]
    assert len(content) == 1 and content[0]["type"] == "text" and "REVIEW CONTEXT" in content[0]["text"]
    rows = tools.db.conn.execute("SELECT trade_id, kind FROM ai_analysis").fetchall()
    assert [(r_["trade_id"], r_["kind"]) for r_ in rows] == [(1, "review")]     # no vision row
    assert "Review of trade #1" in C.format_review(r) and "CHART" not in C.format_review(r)
    assert "error" in asyncio.run(coach.review(999))


def test_review_attaches_screenshots_as_images(tools, tmp_path):
    pre = tmp_path / "1_PRE.png"; pre.write_bytes(b"\x89PNG\r\n\x1a\nfakepng")
    post = tmp_path / "1_POST.jpg"; post.write_bytes(b"\xff\xd8fakejpg")
    big = tmp_path / "1_POST_big.png"; big.write_bytes(b"\x89PNG" + b"0" * (C.MAX_IMAGE_BYTES + 1))
    tools.add_screenshot(1, "POST", str(post))          # POST registered first — must still come after PRE
    tools.add_screenshot(1, "PRE", str(pre))
    tools.add_screenshot(1, "POST", str(big))
    tools.add_screenshot(1, "POST", str(tmp_path / "missing.png"))
    tools.add_screenshot(1, "PRE", str(tmp_path / "notes.txt"))
    fake = FakeClient(review={**_REVIEW, "chart_observations": ["POST chart shows a close back below the line"]})
    coach = C.Coach(tools, AiCfg(), client=fake)
    r = asyncio.run(coach.review(1))
    assert r["images_used"] == ["1_PRE.png", "1_POST.jpg"]
    assert any("over 5 MB" in s for s in r["images_skipped"])
    assert any("file missing" in s for s in r["images_skipped"])
    assert any("unsupported type" in s for s in r["images_skipped"])
    content = fake.calls[0]["messages"][0]["content"]
    types = [c["type"] for c in content]
    assert types == ["text", "image", "text", "image", "text"]          # label, image, ..., context last
    assert content[1]["source"]["media_type"] == "image/png" and content[3]["source"]["media_type"] == "image/jpeg"
    import base64
    assert base64.b64decode(content[1]["source"]["data"]).startswith(b"\x89PNG")
    assert "PRE" in content[0]["text"] and "POST" in content[2]["text"]
    assert "chart_observations" in fake.calls[0]["system"][0]["text"]
    kinds = [row["kind"] for row in tools.db.conn.execute("SELECT kind FROM ai_analysis ORDER BY id")]
    assert kinds == ["review", "vision"]
    vision = tools.db.conn.execute("SELECT input_metrics, output FROM ai_analysis WHERE kind='vision'").fetchone()
    assert '"label": "observation"' in vision["input_metrics"] and "below the line" in vision["output"]
    txt = C.format_review(r)
    assert "CHART (observations, not facts)" in txt and "screenshots skipped" in txt
    # opt out
    fake2 = FakeClient(review=_REVIEW)
    asyncio.run(C.Coach(tools, AiCfg(), client=fake2).review(1, with_images=False, store=False))
    assert [c["type"] for c in fake2.calls[0]["messages"][0]["content"]] == ["text"]


# ── Gemini backend ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("provider,model,env,expected", [
    ("gemini", "claude-opus-5", {}, True),
    ("anthropic", "gemini-3.6-flash", {"GEMINI_API_KEY": "x"}, False),
    ("auto", "gemini-3.6-flash", {"ANTHROPIC_API_KEY": "x"}, True),
    ("auto", "claude-opus-5", {"GEMINI_API_KEY": "x"}, True),
    ("auto", "claude-opus-5", {"GEMINI_API_KEY": "x", "ANTHROPIC_API_KEY": "x"}, False),
    ("auto", "claude-opus-5", {}, False),
])
def test_provider_resolution(tools, monkeypatch, provider, model, env, expected):
    for var in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    coach = C.Coach(tools, AiCfg(provider=provider, model=model), client=SimpleNamespace())
    assert coach.is_gemini is expected


def test_gemini_tools_mirror_the_read_only_surface(tools):
    anthropic_names = {t.name for t in C.Coach(tools, AiCfg(), client=FakeClient()).build_tools()}
    gem = C.Coach(tools, _gemini(), client=FakeGemini()).build_tools()
    assert {f.__name__ for f in gem} == anthropic_names
    for f in gem:
        assert f.__doc__ and inspect.iscoroutinefunction(f), f.__name__


def test_gemini_ask_runs_tools_stores_and_caps_calls(tools):
    fake = FakeGemini(
        script=[("journal_stats", {"period": "all"}), ("journal_tag_stats", {"phase": "ENTRY"})],
        answer="YOUR HISTORY: 3 trades (n=3, small sample), 67% win, avg +0.83R. FOMO: 0/1. YOUR DECISION.",
    )
    coach = C.Coach(tools, _gemini(max_tool_calls=5), client=fake)
    ans = asyncio.run(coach.ask("how am I doing on 4H breaks?", signal={"event": "break_up", "rsi": 68}))
    assert [c.name for c in ans.tool_calls] == ["journal_stats", "journal_tag_stats"]
    assert ans.tool_calls[0].result["n"] == 3
    assert ans.unverified_numbers == []
    assert ans.usage == {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150}
    kw = fake.calls[0]
    assert kw["model"] == "gemini-3.6-flash"
    assert kw["config"]["system_instruction"] == prompts.COACH_V1
    assert kw["config"]["automatic_function_calling"] == {"maximum_remote_calls": 5}
    assert fake.sent[0].startswith("how am I doing") and '"rsi": 68' in fake.sent[0]
    row = tools.db.conn.execute("SELECT kind, model, input_metrics FROM ai_analysis").fetchone()
    assert row["kind"] == "suggestion" and row["model"] == "gemini-3.6-flash"
    assert json.loads(row["input_metrics"])["tool_calls"][0]["name"] == "journal_stats"


def test_gemini_ask_flags_invented_numbers_and_respects_budget(tools):
    fake = FakeGemini(script=[("journal_stats", {})], answer="Your win rate is 80% (n=3) with 9.9R expectancy.")
    coach = C.Coach(tools, _gemini(daily_ask_limit=1), client=fake)
    assert asyncio.run(coach.ask("?")).unverified_numbers == ["80", "9.9"]
    with pytest.raises(C.AskBudgetExceeded):
        asyncio.run(coach.ask("again"))


def test_gemini_review_structured_and_stored(tools):
    pytest.importorskip("google.genai")
    fake = FakeGemini(text=json.dumps(_REVIEW))
    r = asyncio.run(C.Coach(tools, _gemini(), client=fake).review(1))
    assert r["trade_id"] == 1 and r["facts"] == _REVIEW["facts"] and r["prompt_version"] == "review_v1"
    assert r["unverified_numbers"] == [] and r["analysis_id"] is not None
    kw = fake.calls[0]
    assert kw["config"]["response_mime_type"] == "application/json"
    assert set(kw["config"]["response_schema"].model_fields) == set(_REVIEW)
    assert "No chart screenshots" in kw["config"]["system_instruction"]
    assert kw["contents"][-1].startswith("REVIEW CONTEXT")
    # a response that isn't the schema is reported, not stored
    bad = asyncio.run(C.Coach(tools, _gemini(), client=FakeGemini(text="not json")).review(1))
    assert bad["error"] == "failed to parse structured review" and "analysis_id" not in bad
    kinds = [row["kind"] for row in tools.db.conn.execute("SELECT kind FROM ai_analysis")]
    assert kinds == ["review"]


def test_gemini_narrative(tools):
    fake = FakeGemini(text="3 trades this week (n=3), and a 12% edge.")
    text, missing, model = asyncio.run(C.Coach(tools, _gemini(), client=fake).narrative("sys", {"n": 3}))
    assert text.startswith("3 trades") and missing == ["12"] and model == "gemini-3.6-flash"
    kw = fake.calls[0]
    assert kw["config"] == {"system_instruction": "sys", "max_output_tokens": 4000}


# ── fallback model on rate limit ─────────────────────────────────────────────
def _rate_limit_error():
    from google.genai import errors
    return errors.ClientError(429, {"error": {"message": "quota", "status": "RESOURCE_EXHAUSTED"}})


def test_ask_falls_back_to_reserve_model_on_rate_limit(tools):
    fake = FakeGemini(
        script=[("journal_stats", {"period": "all"})], answer="3 trades (n=3).",
        errors={"gemini-3.6-flash": _rate_limit_error()},
    )
    cfg = _gemini(fallback_model="gemini-3.5-flash-lite")
    ans = asyncio.run(C.Coach(tools, cfg, client=fake).ask("how am I doing?"))
    assert ans.model == "gemini-3.5-flash-lite"
    assert [c.name for c in ans.tool_calls] == ["journal_stats"]     # not doubled by the retry
    assert [kw["model"] for kw in fake.calls] == ["gemini-3.6-flash", "gemini-3.5-flash-lite"]
    row = tools.db.conn.execute("SELECT model FROM ai_analysis").fetchone()
    assert row["model"] == "gemini-3.5-flash-lite"


def test_ask_raises_when_fallback_also_rate_limited(tools):
    fake = FakeGemini(errors={
        "gemini-3.6-flash": _rate_limit_error(),
        "gemini-3.5-flash-lite": _rate_limit_error(),
    })
    cfg = _gemini(fallback_model="gemini-3.5-flash-lite")
    with pytest.raises(C.CoachError, match="rate limit"):
        asyncio.run(C.Coach(tools, cfg, client=fake).ask("?"))


def test_ask_does_not_fall_back_without_a_configured_fallback(tools):
    fake = FakeGemini(errors={"gemini-3.6-flash": _rate_limit_error()})
    with pytest.raises(C.CoachError, match="rate limit"):
        asyncio.run(C.Coach(tools, _gemini(), client=fake).ask("?"))
    assert len(fake.calls) == 1


def test_ask_does_not_fall_back_on_a_non_rate_limit_error(tools):
    from google.genai import errors
    fake = FakeGemini(errors={
        "gemini-3.6-flash": errors.ClientError(401, {"error": {"message": "bad key"}}),
    })
    cfg = _gemini(fallback_model="gemini-3.5-flash-lite")
    with pytest.raises(C.CoachError, match="API key rejected"):
        asyncio.run(C.Coach(tools, cfg, client=fake).ask("?"))
    assert len(fake.calls) == 1     # no retry for a non-429 error


def test_review_falls_back_to_reserve_model_on_rate_limit(tools):
    fake = FakeGemini(text=json.dumps(_REVIEW), errors={"gemini-3.6-flash": _rate_limit_error()})
    cfg = _gemini(fallback_model="gemini-3.5-flash-lite")
    r = asyncio.run(C.Coach(tools, cfg, client=fake).review(1))
    assert r["model"] == "gemini-3.5-flash-lite" and r["facts"] == _REVIEW["facts"]
    row = tools.db.conn.execute("SELECT model FROM ai_analysis").fetchone()
    assert row["model"] == "gemini-3.5-flash-lite"


def test_narrative_falls_back_to_reserve_model_on_rate_limit(tools):
    fake = FakeGemini(text="3 trades (n=3).", errors={"gemini-3.6-flash": _rate_limit_error()})
    cfg = _gemini(fallback_model="gemini-3.5-flash-lite")
    text, missing, model = asyncio.run(C.Coach(tools, cfg, client=fake).narrative("sys", {"n": 3}))
    assert text == "3 trades (n=3)." and model == "gemini-3.5-flash-lite"


def test_fallback_never_applies_to_the_anthropic_provider(tools):
    """A fallback_model is Gemini-only; the Anthropic path must ignore it."""
    fake = FakeClient(answer="ok")
    cfg = AiCfg(provider="anthropic", fallback_model="gemini-3.5-flash-lite")
    ans = asyncio.run(C.Coach(tools, cfg, client=fake).ask("?"))
    assert ans.model == "claude-opus-5"
