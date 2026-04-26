"""
mcp_client.py — Async Claude MCP Client for the AlgoTrader ETF Bot.

This module:
1. Connects to the local MCP server (mcp_server.py)
2. Sends a structured market context prompt to Claude
3. Returns Claude's structured trade decision: action, instrument, confidence, reasoning

Usage (standalone test):
    python mcp_client.py
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import Optional

import anthropic
import httpx

import config

logger = logging.getLogger(__name__)

# ─── Claude system prompt ─────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are an expert Indian ETF trader and quantitative analyst.
Your job is to analyse real-time market data and make precise BUY / SELL / HOLD 
decisions for an automated trading bot operating on the Upstox platform.

## Your Decision Rules

**BUY:**
- Only buy when BOTH momentum (ETF rank) AND technical signals (RSI not overbought, 
  EMA uptrend, MACD bullish) confirm an opportunity.
- Only buy if there are available portfolio slots.
- Do not buy if an ETF is already held.
- Confidence must reflect genuine analysis, not guessing.

**SELL (profit):**
- Recommend SELL when the position has reached or exceeded the profit target %.
- Consider early sell if technical indicators show trend reversal.

**SELL (loss protection):**
- Recommend SELL if RSI shows extreme overbought AND MACD has turned bearish.
- The hard stop-loss (force sell at -3%) is handled separately by the bot engine.

**HOLD:**
- Default to HOLD when signals are mixed or insufficient data is available.
- Confidence < 70% → always HOLD.

## Tools Available
You MUST call the following tools before making your decision:
1. `get_market_summary` — understand portfolio state and available capital
2. `get_etf_rankings` — see today's ranked ETF list
3. `get_portfolio_state` — see current holdings with live P&L
4. `get_technical_indicators` — for each top-ranked ETF and each held ETF
5. `execute_buy` or `execute_sell` — ONLY after your analysis is complete

## Output Format
After calling tools and completing your analysis, respond with a JSON block:

```json
{
  "action": "BUY | SELL | HOLD",
  "instrument": "instrument_token_or_null",
  "confidence": 0.0,
  "reasoning": "step-by-step explanation",
  "signals": {
    "momentum": "BULLISH | BEARISH | NEUTRAL",
    "rsi": "value or null",
    "ema_trend": "UPTREND | DOWNTREND | INSUFFICIENT_DATA",
    "macd": "BULLISH | BEARISH | null"
  }
}
```

Be conservative. Protecting capital is more important than chasing profits.
"""


class MCPClient:
    """
    Async client that:
    - Talks to Claude via the Anthropic SDK with tool use
    - Routes tool calls to the local MCP server via HTTP
    - Returns the final structured trade decision
    """

    def __init__(self):
        self.claude = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        self.mcp_url = config.MCP_SERVER_URL
        self.model = config.CLAUDE_MODEL
        self._mcp_tools: list[dict] | None = None

    async def _fetch_mcp_tool_schemas(self) -> list[dict]:
        """Fetch tool definitions from the MCP server's OpenAPI schema."""
        # We define them statically here to match mcp_server.py exactly,
        # avoiding an extra round-trip on each decision cycle.
        return [
            {
                "name": "get_live_prices",
                "description": (
                    "Fetch the Last Traded Price (LTP) for one or more ETF "
                    "instrument tokens from Upstox in real time."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "instrument_tokens": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of Upstox instrument token strings.",
                        }
                    },
                    "required": ["instrument_tokens"],
                },
            },
            {
                "name": "get_portfolio_state",
                "description": (
                    "Return current portfolio holdings with live prices and P&L."
                ),
                "input_schema": {"type": "object", "properties": {}},
            },
            {
                "name": "get_etf_rankings",
                "description": (
                    "Fetch today's ETF rankings and prices from the Google Sheet."
                ),
                "input_schema": {"type": "object", "properties": {}},
            },
            {
                "name": "get_technical_indicators",
                "description": (
                    "Compute RSI, EMA-9, EMA-21, and MACD for a given ETF instrument token."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "instrument_token": {
                            "type": "string",
                            "description": "Upstox instrument token string.",
                        }
                    },
                    "required": ["instrument_token"],
                },
            },
            {
                "name": "get_market_summary",
                "description": (
                    "High-level portfolio summary: holdings, capital available, P&L."
                ),
                "input_schema": {"type": "object", "properties": {}},
            },
            {
                "name": "execute_buy",
                "description": (
                    "Place a BUY market order. DRY_RUN mode simulates without real orders."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "instrument_token": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["instrument_token", "reason"],
                },
            },
            {
                "name": "execute_sell",
                "description": (
                    "Place a SELL market order. DRY_RUN mode simulates without real orders."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "instrument_token": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["instrument_token", "reason"],
                },
            },
        ]

    async def _call_mcp_tool(self, tool_name: str, tool_input: dict) -> str:
        """Forward a tool call from Claude to the MCP server via HTTP POST."""
        url = f"{self.mcp_url}/tool/{tool_name}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                resp = await client.post(url, json=tool_input)
                resp.raise_for_status()
                data = resp.json()
                # MCP server returns list[TextContent]; extract text
                if isinstance(data, list) and data:
                    return data[0].get("text", json.dumps(data))
                return json.dumps(data)
            except httpx.HTTPStatusError as e:
                logger.error(f"MCP tool call failed [{tool_name}]: {e}")
                return json.dumps({"error": str(e)})
            except Exception as e:
                logger.error(f"MCP tool call error [{tool_name}]: {e}")
                return json.dumps({"error": str(e)})

    async def get_trade_decision(self) -> dict:
        """
        Main entry point: ask Claude to analyse the market and return a
        structured trade decision dict.

        Returns:
            {
                "action": "BUY" | "SELL" | "HOLD",
                "instrument": str | None,
                "confidence": float,
                "reasoning": str,
                "signals": dict,
                "timestamp": str,
            }
        """
        tools = await self._fetch_mcp_tool_schemas()
        messages = [
            {
                "role": "user",
                "content": (
                    f"Current time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S IST')}\n"
                    "Please analyse the current market and portfolio using the available tools "
                    "and provide your trade decision."
                ),
            }
        ]

        logger.info("Querying Claude for trade decision...")

        # ── Agentic tool-use loop ─────────────────────────────────────────────
        max_iterations = 10  # prevent runaway loops
        for iteration in range(max_iterations):
            response = self.claude.messages.create(
                model=self.model,
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                tools=tools,
                messages=messages,
            )

            # Append Claude's response to the message chain
            messages.append({"role": "assistant", "content": response.content})

            # ── If Claude wants to use tools ──────────────────────────────────
            if response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        logger.info(f"Claude calling tool: {block.name}({block.input})")
                        result_text = await self._call_mcp_tool(block.name, block.input)
                        logger.debug(f"Tool result [{block.name}]: {result_text[:200]}...")
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": result_text,
                            }
                        )

                messages.append({"role": "user", "content": tool_results})
                continue  # next iteration: Claude processes tool results

            # ── Claude has finished (stop_reason == "end_turn") ───────────────
            elif response.stop_reason == "end_turn":
                final_text = ""
                for block in response.content:
                    if hasattr(block, "text"):
                        final_text += block.text

                decision = self._parse_decision(final_text)
                logger.info(
                    f"Claude decision: {decision['action']} "
                    f"| {decision['instrument']} "
                    f"| confidence={decision['confidence']:.0%}"
                )
                return decision

            else:
                logger.warning(f"Unexpected stop_reason: {response.stop_reason}")
                break

        # Fallback if max iterations hit
        return self._hold_decision("Max tool-use iterations reached")

    def _parse_decision(self, text: str) -> dict:
        """Extract the JSON decision block from Claude's final response."""
        import re

        # Try to find a ```json ... ``` block
        match = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(1))
                return self._normalise_decision(data)
            except json.JSONDecodeError:
                pass

        # Try to find a raw JSON object
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(0))
                return self._normalise_decision(data)
            except json.JSONDecodeError:
                pass

        logger.warning(f"Could not parse Claude's response as JSON:\n{text[:300]}")
        return self._hold_decision(f"Parse failure — raw response: {text[:200]}")

    def _normalise_decision(self, data: dict) -> dict:
        """Ensure the decision dict has all required fields with correct types."""
        action = str(data.get("action", "HOLD")).upper()
        if action not in ("BUY", "SELL", "HOLD"):
            action = "HOLD"
        return {
            "action": action,
            "instrument": data.get("instrument") or data.get("instrument_token"),
            "confidence": float(data.get("confidence", 0.0)),
            "reasoning": str(data.get("reasoning", "")),
            "signals": data.get("signals", {}),
            "timestamp": datetime.now().isoformat(),
        }

    def _hold_decision(self, reason: str) -> dict:
        return {
            "action": "HOLD",
            "instrument": None,
            "confidence": 0.0,
            "reasoning": reason,
            "signals": {},
            "timestamp": datetime.now().isoformat(),
        }


# ─── Standalone test ──────────────────────────────────────────────────────────
async def _test():
    logging.basicConfig(level=logging.INFO)
    client = MCPClient()
    decision = await client.get_trade_decision()
    print("\n" + "=" * 60)
    print("CLAUDE'S TRADE DECISION")
    print("=" * 60)
    print(json.dumps(decision, indent=2))


if __name__ == "__main__":
    asyncio.run(_test())
