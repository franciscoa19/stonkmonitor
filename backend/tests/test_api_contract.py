"""The dashboard's requests must hit routes that exist and return what it reads.

Router-level tests: a bare FastAPI app with the API router and no lifespan, so
no background task, broker or network is involved.
"""
import re
from pathlib import Path
from types import SimpleNamespace as NS

import httpx
import pytest
from fastapi import FastAPI

from api.routes import router
from test_execution_safety import database  # noqa: F401  (fixture)

FRONTEND_SRC = Path(__file__).resolve().parents[2] / "frontend" / "src"


@pytest.fixture
def app():
    application = FastAPI()
    application.include_router(router, prefix="/api")
    return application


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost")


def frontend_api_paths() -> dict[str, str]:
    """Every `${API}/api/...` path the frontend builds, mapped to its source file.
    Query strings are dropped and `${...}` segments become a placeholder."""
    found = {}
    for source in sorted(FRONTEND_SRC.rglob("*.ts*")):
        for match in re.finditer(r"\$\{API\}(/api/[^`'\"\s]*)", source.read_text(encoding="utf-8")):
            path = re.sub(r"\$\{[^}]*\}", "X", match.group(1).split("?")[0])
            found.setdefault(path, source.name)
    return found


def ts_interface_fields(component: str, interface: str) -> set[str]:
    source = (FRONTEND_SRC / "components" / component).read_text(encoding="utf-8")
    body = re.search(rf"interface {interface} \{{([^}}]*)\}}", source).group(1)
    return set(re.findall(r"^\s*(\w+)\??:", body, flags=re.M))


def test_every_frontend_api_url_is_a_registered_route(app):
    """History called /api/db/signals/stats and /api/db/signals/top-tickers,
    which did not exist; it swallowed the 404s and rendered nothing."""
    from starlette.routing import Match

    def registered(path: str) -> bool:
        return any(route.matches({"type": "http", "path": path, "method": method})[0] == Match.FULL
                   for route in app.routes for method in ("GET", "POST", "DELETE", "PUT"))

    assert registered("/api/account") and not registered("/api/no/such/route")
    paths = frontend_api_paths()
    assert len(paths) >= 10, "the URL scan found too little — has the fetch style changed?"
    assert {path: source for path, source in paths.items() if not registered(path)} == {}


async def seed_signal(db, ticker, score, side):
    await db._exec(
        """INSERT INTO signals (type, ticker, score, side, title, description, created_at)
           VALUES ('sweep', ?, ?, ?, 't', 'd', ?)""",
        (ticker, score, side, f"2026-10-0{int(score) % 9 + 1}T14:00:00"), strict=True)


async def test_history_endpoints_return_the_fields_the_component_reads(app, database, monkeypatch):
    import main
    monkeypatch.setattr(main, "db", database)
    async with client(app) as http:
        empty = (await http.get("/api/db/signals/stats")).json()
        assert (empty["total"], empty["elite"], empty["avg_score"]) == (0, 0, None)
        assert (await http.get("/api/db/signals/top-tickers")).json() == []

        for ticker, score, side in (("AAPL", 9.5, "bullish"), ("AAPL", 7.0, "bearish"),
                                    ("NVDA", 8.0, "bullish"), ("MSFT", 6.0, "neutral")):
            await seed_signal(database, ticker, score, side)

        response = await http.get("/api/db/signals/stats")
        assert response.status_code == 200
        stats = response.json()
        assert ts_interface_fields("History.tsx", "Stats") <= stats.keys()
        assert (stats["total"], stats["elite"], stats["high"], stats["bull"], stats["bear"]) == (4, 1, 3, 2, 1)
        assert stats["avg_score"] == pytest.approx(7.625) and stats["last_signal"]

        response = await http.get("/api/db/signals/top-tickers?limit=2")
        assert response.status_code == 200
        top = response.json()
        assert [row["ticker"] for row in top] == ["AAPL", "NVDA"]      # count, then max score
        assert ts_interface_fields("History.tsx", "TopTicker") <= top[0].keys()
        assert (top[0]["signal_count"], top[0]["max_score"], top[0]["bull_count"], top[0]["bear_count"]) == (2, 9.5, 1, 1)


@pytest.mark.parametrize("broker_reply", [{}, {"error": "unauthorized"}, {"cash": 1.0}])
async def test_account_fetch_failure_is_a_503_not_an_empty_account(app, monkeypatch, broker_reply):
    """get_account() returns {} when the broker call fails. As an HTTP 200 the
    dashboard stored it and crashed on account.equity.toLocaleString()."""
    import main
    monkeypatch.setattr(main, "trader", NS(get_account=lambda: broker_reply))
    async with client(app) as http:
        assert (await http.get("/api/account")).status_code == 503


async def test_account_success_passes_through(app, monkeypatch):
    import main
    account = {"equity": 52156.97, "cash": 52156.97, "buying_power": 208627.88,
               "day_trade_count": 0, "pdt_flag": False, "status": "ACTIVE"}
    monkeypatch.setattr(main, "trader", NS(get_account=lambda: account))
    async with client(app) as http:
        response = await http.get("/api/account")
    assert response.status_code == 200 and response.json() == account
