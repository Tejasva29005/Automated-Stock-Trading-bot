"""
mcp_server.py — Claude MCP Server for the AlgoTrader ETF Bot.

This server exposes real market data and portfolio tools to Claude via the
Model Context Protocol (MCP). Claude calls these tools during its reasoning
chain before returning a BUY / SELL / HOLD decision.

Run this server first:
    python mcp_server.py

Then start the main bot:
    python main.py
"""

import json
import logging
import math
import os
import asyncio
from datetime import datetime, timedelta
from typing import Any

import uvicorn
import upstox_client
import gspread
from fastapi import FastAPI
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import Tool, TextContent
from oauth2client.service_account import ServiceAccountCredentials
from starlette.routing import Route

import config

# ─── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [MCP-SERVER] %(levelname)s — %(message)s",
)
logger = logging.getLogger(__name__)

# ─── Upstox clients (module-level, reused across tool calls) ─────────────────
_upstox_cfg = upstox_client.Configuration()
_upstox_cfg.access_token = config.UPSTOX_ACCESS_TOKEN
_api_client = upstox_client.ApiClient(_upstox_cfg)
market_api = upstox_client.MarketQuoteApi(_api_client)
order_api = upstox_client.OrderApi(_api_client)

# ─── Google Sheets client ─────────────────────────────────────────────────────
_scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]
try:
    _creds = ServiceAccountCredentials.from_json_keyfile_name(
        config.GOOGLE_CREDENTIALS_PATH, _scope
    )
    _gc = gspread.authorize(_creds)
    _worksheet = _gc.open_by_key(config.SPREADSHEET_ID).get_worksheet(0)
    logger.info("Google Sheets connected ✓")
except Exception as e:
    _worksheet = None
    logger.warning(f"Google Sheets not available: {e}")

# ─── In-memory price history for technical indicators ─────────────────────────
# { instrument_token: [(timestamp, price), ...] }
_price_history: dict[str, list[tuple[datetime, float]]] = {}
_HISTORY_WINDOW = 30  # keep last 30 data-points per instrument


def _record_price(token: str, price: float):
    """Append a price observation to the in-memory history buffer."""
    history = _price_history.setdefault(token, [])
    history.append((datetime.now(), price))
    # keep only the last N observations
    if len(history) > _HISTORY_WINDOW:
        _price_history[token] = history[-_HISTORY_WINDOW:]


# ─── Helper utilities ─────────────────────────────────────────────────────────

def _load_state() -> dict:
    """Load portfolio state from JSON file."""
    if not os.path.exists(config.STATE_PATH):
        return {}
    with open(config.STATE_PATH, "r") as f:
        return json.load(f)


def _save_state(state: dict):
    """Persist portfolio state to JSON file."""
    with open(config.STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def _compute_rsi(prices: list[float], period: int = 14) -> float | None:
    """Compute RSI for a list of closing prices."""
    if len(prices) < period + 1:
        return None
    deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains = [max(d, 0) for d in deltas]
    losses = [abs(min(d, 0)) for d in deltas]
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def _compute_ema(prices: list[float], period: int) -> float | None:
    """Compute Exponential Moving Average."""
    if len(prices) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for price in prices[period:]:
        ema = price * k + ema * (1 - k)
    return round(ema, 4)


def _compute_macd(
    prices: list[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> dict | None:
    """Compute MACD line, signal line, and histogram."""
    if len(prices) < slow + signal:
        return None
    ema_fast = _compute_ema(prices, fast)
    ema_slow = _compute_ema(prices, slow)
    if ema_fast is None or ema_slow is None:
        return None
    macd_line = round(ema_fast - ema_slow, 4)
    # build macd history to compute signal EMA
    macd_vals = []
    for i in range(slow - 1, len(prices)):
        ef = _compute_ema(prices[: i + 1], fast)
        es = _compute_ema(prices[: i + 1], slow)
        if ef is not None and es is not None:
            macd_vals.append(ef - es)
    signal_line = _compute_ema(macd_vals, signal)
    if signal_line is None:
        return None
    histogram = round(macd_line - signal_line, 4)
    return {
        "macd": macd_line,
        "signal": round(signal_line, 4),
        "histogram": histogram,
        "trend": "BULLISH" if histogram > 0 else "BEARISH",
    }


# ─── MCP Server Setup ─────────────────────────────────────────────────────────
server = Server("algotrader-mcp")


# ─── Tool: get_live_prices ────────────────────────────────────────────────────
@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="get_live_prices",
            description=(
                "Fetch the Last Traded Price (LTP) for one or more ETF "
                "instrument tokens from Upstox in real time."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "instrument_tokens": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "List of Upstox instrument token strings, "
                            "e.g. ['NSE_EQ|INE0J1Y01017']"
                        ),
                    }
                },
                "required": ["instrument_tokens"],
            },
        ),
        Tool(
            name="get_portfolio_state",
            description=(
                "Return the current portfolio: all held ETFs with their "
                "average buy price, quantity, buy date, current live price, "
                "and P&L percentage."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="get_etf_rankings",
            description=(
                "Fetch today's ETF rankings and sheet prices from the "
                "Google Spreadsheet. Returns a list of dicts with 'code' "
                "and 'sheet_price'."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="get_technical_indicators",
            description=(
                "Compute RSI, EMA-9, EMA-21, and MACD for a given ETF "
                "instrument token using the in-memory price history. "
                "Returns None fields if not enough data has been collected yet."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "instrument_token": {
                        "type": "string",
                        "description": "Upstox instrument token string.",
                    }
                },
                "required": ["instrument_token"],
            },
        ),
        Tool(
            name="get_market_summary",
            description=(
                "Return a high-level market summary: number of holdings, "
                "total corpus, available capital, and overall portfolio P&L."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="execute_buy",
            description=(
                "Place a BUY market order for a given ETF instrument token. "
                "In DRY_RUN mode this is simulated and no real order is placed. "
                "Provide a reason for the trade for audit logging."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "instrument_token": {
                        "type": "string",
                        "description": "Upstox instrument token.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Human-readable reason for the buy.",
                    },
                },
                "required": ["instrument_token", "reason"],
            },
        ),
        Tool(
            name="execute_sell",
            description=(
                "Place a SELL market order for a given ETF instrument token. "
                "In DRY_RUN mode this is simulated and no real order is placed. "
                "Provide a reason for the trade for audit logging."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "instrument_token": {
                        "type": "string",
                        "description": "Upstox instrument token.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Human-readable reason for the sell.",
                    },
                },
                "required": ["instrument_token", "reason"],
            },
        ),
    ]


# ─── Tool Implementations ─────────────────────────────────────────────────────
@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """Dispatch incoming tool calls from Claude to the appropriate handler."""

    # ── get_live_prices ───────────────────────────────────────────────────────
    if name == "get_live_prices":
        tokens: list[str] = arguments["instrument_tokens"]
        results = {}
        for token in tokens:
            try:
                quote = market_api.ltp(token, "2.0")
                price = float(quote.data[token].ltp)
                results[token] = {"ltp": price, "status": "ok"}
                _record_price(token, price)  # feed into indicator history
            except Exception as e:
                results[token] = {"ltp": None, "status": f"error: {e}"}
        return [TextContent(type="text", text=json.dumps(results, indent=2))]

    # ── get_portfolio_state ───────────────────────────────────────────────────
    elif name == "get_portfolio_state":
        state = _load_state()
        enriched = {}
        for code, info in state.items():
            try:
                quote = market_api.ltp(code, "2.0")
                live = float(quote.data[code].ltp)
                _record_price(code, live)
            except Exception:
                live = None
            buy = info.get("buy_price", 0)
            qty = info.get("quantity", 1)
            pnl = (
                round((live - buy) / buy * 100, 2)
                if live and buy
                else None
            )
            enriched[code] = {
                **info,
                "live_price": live,
                "pnl_pct": pnl,
                "value": round(live * qty, 2) if live else None,
            }
        return [TextContent(type="text", text=json.dumps(enriched, indent=2))]

    # ── get_etf_rankings ─────────────────────────────────────────────────────
    elif name == "get_etf_rankings":
        if _worksheet is None:
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"error": "Google Sheets not configured"}),
                )
            ]
        try:
            etf_codes = _worksheet.col_values(1)[1:31]
            prices_raw = _worksheet.col_values(3)[1:31]
            rankings = []
            for i, code in enumerate(etf_codes):
                if not code.strip():
                    continue
                try:
                    price = float(str(prices_raw[i]).replace(",", "").strip())
                except (ValueError, IndexError):
                    price = None
                rankings.append(
                    {"rank": i + 1, "code": code.strip(), "sheet_price": price}
                )
            return [
                TextContent(type="text", text=json.dumps(rankings, indent=2))
            ]
        except Exception as e:
            return [
                TextContent(
                    type="text", text=json.dumps({"error": str(e)})
                )
            ]

    # ── get_technical_indicators ──────────────────────────────────────────────
    elif name == "get_technical_indicators":
        token: str = arguments["instrument_token"]
        history = _price_history.get(token, [])
        prices = [p for _, p in history]

        rsi = _compute_rsi(prices)
        ema9 = _compute_ema(prices, 9)
        ema21 = _compute_ema(prices, 21)
        macd = _compute_macd(prices)

        # Simple trend signal
        trend_signal = "INSUFFICIENT_DATA"
        if ema9 is not None and ema21 is not None:
            trend_signal = "UPTREND" if ema9 > ema21 else "DOWNTREND"

        # RSI interpretation
        rsi_signal = "NEUTRAL"
        if rsi is not None:
            if rsi < 30:
                rsi_signal = "OVERSOLD"
            elif rsi > 70:
                rsi_signal = "OVERBOUGHT"

        result = {
            "instrument_token": token,
            "data_points": len(prices),
            "current_price": prices[-1] if prices else None,
            "rsi": rsi,
            "rsi_signal": rsi_signal,
            "ema_9": ema9,
            "ema_21": ema21,
            "ema_trend": trend_signal,
            "macd": macd,
            "last_updated": datetime.now().isoformat(),
        }
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    # ── get_market_summary ────────────────────────────────────────────────────
    elif name == "get_market_summary":
        state = _load_state()
        holdings_count = len(state)
        available_slots = config.MAX_HOLDINGS - holdings_count

        total_invested = 0.0
        total_current = 0.0
        for code, info in state.items():
            buy = info.get("buy_price", 0)
            qty = info.get("quantity", 1)
            total_invested += buy * qty
            try:
                quote = market_api.ltp(code, "2.0")
                live = float(quote.data[code].ltp)
                total_current += live * qty
            except Exception:
                total_current += buy * qty  # fallback: assume no change

        overall_pnl_pct = (
            round((total_current - total_invested) / total_invested * 100, 2)
            if total_invested > 0
            else 0.0
        )
        per_slot_budget = round(config.TOTAL_CORPUS / config.MAX_HOLDINGS, 2)
        available_capital = round(per_slot_budget * available_slots, 2)

        summary = {
            "holdings_count": holdings_count,
            "max_holdings": config.MAX_HOLDINGS,
            "available_slots": available_slots,
            "total_corpus": config.TOTAL_CORPUS,
            "total_invested": round(total_invested, 2),
            "total_current_value": round(total_current, 2),
            "overall_pnl_pct": overall_pnl_pct,
            "available_capital": available_capital,
            "per_slot_budget": per_slot_budget,
            "profit_target_pct": config.PROFIT_TARGET * 100,
            "hard_stop_loss_pct": config.HARD_STOP_LOSS_PCT * 100,
            "dry_run": config.DRY_RUN,
            "timestamp": datetime.now().isoformat(),
        }
        return [TextContent(type="text", text=json.dumps(summary, indent=2))]

    # ── execute_buy ───────────────────────────────────────────────────────────
    elif name == "execute_buy":
        token: str = arguments["instrument_token"]
        reason: str = arguments.get("reason", "Claude AI decision")
        state = _load_state()

        if len(state) >= config.MAX_HOLDINGS:
            msg = {
                "status": "skipped",
                "reason": f"Portfolio full ({len(state)}/{config.MAX_HOLDINGS})",
            }
            return [TextContent(type="text", text=json.dumps(msg))]

        if token in state:
            msg = {"status": "skipped", "reason": f"{token} already held"}
            return [TextContent(type="text", text=json.dumps(msg))]

        # Fetch live price
        try:
            quote = market_api.ltp(token, "2.0")
            price = float(quote.data[token].ltp)
        except Exception as e:
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"status": "error", "detail": str(e)}),
                )
            ]

        budget = config.TOTAL_CORPUS / config.MAX_HOLDINGS
        quantity = max(1, math.floor(budget / price))

        if config.DRY_RUN:
            logger.info(
                f"[DRY-RUN] BUY  {token}  qty={quantity}  "
                f"price=₹{price:.2f}  reason={reason}"
            )
            result = {
                "status": "dry_run",
                "action": "BUY",
                "instrument_token": token,
                "quantity": quantity,
                "price": price,
                "reason": reason,
                "timestamp": datetime.now().isoformat(),
            }
        else:
            try:
                req = upstox_client.PlaceOrderRequest(
                    quantity=quantity,
                    product="D",
                    validity="DAY",
                    price=0,
                    instrument_token=token,
                    order_type="MARKET",
                    transaction_type="BUY",
                )
                resp = order_api.place_order(req, "2.0")
                order_id = resp.data.order_id
                logger.info(
                    f"BUY  {token}  qty={quantity}  price=₹{price:.2f}  "
                    f"order_id={order_id}  reason={reason}"
                )
                # Persist to state
                state[token] = {
                    "buy_price": round(price, 4),
                    "quantity": quantity,
                    "buy_date": datetime.now().strftime("%Y-%m-%d"),
                    "claude_reason": reason,
                }
                _save_state(state)
                result = {
                    "status": "success",
                    "action": "BUY",
                    "instrument_token": token,
                    "quantity": quantity,
                    "price": price,
                    "order_id": order_id,
                    "reason": reason,
                    "timestamp": datetime.now().isoformat(),
                }
            except Exception as e:
                logger.error(f"BUY order failed {token}: {e}")
                result = {"status": "error", "detail": str(e)}

        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    # ── execute_sell ──────────────────────────────────────────────────────────
    elif name == "execute_sell":
        token: str = arguments["instrument_token"]
        reason: str = arguments.get("reason", "Claude AI decision")
        state = _load_state()

        if token not in state:
            msg = {"status": "skipped", "reason": f"{token} not in portfolio"}
            return [TextContent(type="text", text=json.dumps(msg))]

        quantity = state[token].get("quantity", 1)

        # Fetch live price for logging
        try:
            quote = market_api.ltp(token, "2.0")
            price = float(quote.data[token].ltp)
        except Exception:
            price = None

        if config.DRY_RUN:
            logger.info(
                f"[DRY-RUN] SELL {token}  qty={quantity}  "
                f"price=₹{price}  reason={reason}"
            )
            result = {
                "status": "dry_run",
                "action": "SELL",
                "instrument_token": token,
                "quantity": quantity,
                "price": price,
                "reason": reason,
                "timestamp": datetime.now().isoformat(),
            }
        else:
            try:
                req = upstox_client.PlaceOrderRequest(
                    quantity=quantity,
                    product="D",
                    validity="DAY",
                    price=0,
                    instrument_token=token,
                    order_type="MARKET",
                    transaction_type="SELL",
                )
                resp = order_api.place_order(req, "2.0")
                order_id = resp.data.order_id
                logger.info(
                    f"SELL {token}  qty={quantity}  price=₹{price}  "
                    f"order_id={order_id}  reason={reason}"
                )
                buy_price = state[token].get("buy_price", 0)
                pnl_pct = (
                    round((price - buy_price) / buy_price * 100, 2)
                    if price and buy_price
                    else None
                )
                del state[token]
                _save_state(state)
                result = {
                    "status": "success",
                    "action": "SELL",
                    "instrument_token": token,
                    "quantity": quantity,
                    "price": price,
                    "pnl_pct": pnl_pct,
                    "order_id": order_id,
                    "reason": reason,
                    "timestamp": datetime.now().isoformat(),
                }
            except Exception as e:
                logger.error(f"SELL order failed {token}: {e}")
                result = {"status": "error", "detail": str(e)}

        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    else:
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": f"Unknown tool: {name}"}),
            )
        ]


# ─── FastAPI + SSE Transport Setup ───────────────────────────────────────────
app = FastAPI(title="AlgoTrader MCP Server", version="1.0.0")
sse_transport = SseServerTransport("/messages")


@app.get("/")
async def root():
    return {
        "service": "AlgoTrader MCP Server",
        "version": "1.0.0",
        "status": "running",
        "dry_run": config.DRY_RUN,
        "tools": [
            "get_live_prices",
            "get_portfolio_state",
            "get_etf_rankings",
            "get_technical_indicators",
            "get_market_summary",
            "execute_buy",
            "execute_sell",
        ],
    }


@app.get("/sse")
async def handle_sse(request):
    """SSE endpoint — Claude MCP client connects here."""
    async with sse_transport.connect_sse(
        request.scope, request.receive, request._send
    ) as streams:
        await server.run(
            streams[0],
            streams[1],
            server.create_initialization_options(),
        )


@app.post("/messages")
async def handle_post_message(request):
    """POST endpoint for MCP message relay."""
    await sse_transport.handle_post_message(
        request.scope, request.receive, request._send
    )


# ─── Entry point ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logger.info(
        f"Starting AlgoTrader MCP Server on "
        f"http://{config.MCP_SERVER_HOST}:{config.MCP_SERVER_PORT}"
    )
    logger.info(f"DRY_RUN mode: {config.DRY_RUN}")
    if config.DRY_RUN:
        logger.warning(
            "⚠️  DRY_RUN=True — no real orders will be placed. "
            "Set DRY_RUN=false in .env for live trading."
        )
    uvicorn.run(
        app,
        host=config.MCP_SERVER_HOST,
        port=config.MCP_SERVER_PORT,
        log_level="info",
    )
