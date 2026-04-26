"""
main.py — AlgoTrader ETF Bot with Claude MCP real-time decision engine.

Two modes are supported:
  1. REALTIME (default): polls Claude every POLL_INTERVAL_SEC seconds
  2. LEGACY: the original once-a-day schedule (pass --legacy flag)

Usage:
  Start the MCP server first:  python mcp_server.py
  Then start the bot:          python main.py
  Legacy mode:                 python main.py --legacy
"""

import asyncio
import logging
import sys
import json
from datetime import datetime

import schedule
import time

import config
from trader import AlgoTrader
from mcp_client import MCPClient

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    filename=config.LOG_PATH,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
console = logging.StreamHandler()
console.setLevel(logging.INFO)
logging.getLogger("").addHandler(console)
logger = logging.getLogger(__name__)


# ─── Dashboard ────────────────────────────────────────────────────────────────
def print_dashboard(trader: AlgoTrader, last_decision: dict | None = None):
    """Print a live portfolio dashboard with Claude's last decision."""
    try:
        summary = trader.get_portfolio_summary()
        width = 65
        print(f"\n{'=' * width}")
        print(f"  ALGOTRADER ETF BOT  ×  CLAUDE AI ENGINE")
        print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  "
              f"{'[DRY-RUN]' if config.DRY_RUN else '[LIVE]'}")
        print(f"{'=' * width}")
        print(f"  Holdings : {len(summary)}/{config.MAX_HOLDINGS} ETFs")
        print(f"  Interval : every {config.POLL_INTERVAL_SEC}s")
        print(f"  Stop-Loss: -{config.HARD_STOP_LOSS_PCT * 100:.1f}%  "
              f"| Profit Target: +{config.PROFIT_TARGET * 100:.1f}%")
        print(f"{'─' * width}")

        if summary:
            print(f"  {'ETF Token':<30} {'Buy ₹':>8} {'Live ₹':>8} {'P&L':>7}  {'Qty':>4}")
            print(f"  {'─' * 30} {'─' * 8} {'─' * 8} {'─' * 7}  {'─' * 4}")
            for code, info in summary.items():
                buy   = info.get("buy_price", 0)
                live  = info.get("live_price") or 0
                pnl   = info.get("pnl_pct")
                qty   = info.get("quantity", 1)
                pnl_s = f"{pnl:+.1f}%" if pnl is not None else "  N/A"
                # Colour hint in terminal (green = profit, red = loss)
                pnl_display = f"\033[92m{pnl_s}\033[0m" if (pnl or 0) >= 0 else f"\033[91m{pnl_s}\033[0m"
                print(f"  {code:<30} {buy:>8.2f} {live:>8.2f} {pnl_display:>16}  {qty:>4}")
        else:
            print("  No holdings yet.")

        if last_decision:
            action = last_decision.get("action", "—")
            conf   = last_decision.get("confidence", 0)
            instr  = last_decision.get("instrument") or "—"
            reason = last_decision.get("reasoning", "")[:80]
            action_colour = {
                "BUY":  "\033[92m",  # green
                "SELL": "\033[91m",  # red
                "HOLD": "\033[93m",  # yellow
            }.get(action, "")
            print(f"{'─' * width}")
            print(f"  Claude  : {action_colour}{action}\033[0m  "
                  f"({conf:.0%} confidence)  → {instr}")
            print(f"  Reason  : {reason}…" if len(reason) == 80 else f"  Reason  : {reason}")

        print(f"{'=' * width}\n")

    except Exception as e:
        logger.warning(f"Dashboard error: {e}")


# ─── Real-time loop ───────────────────────────────────────────────────────────
async def realtime_loop(trader: AlgoTrader, mcp_client: MCPClient):
    """
    Core real-time loop:
      Every POLL_INTERVAL_SEC seconds:
        1. Check hard stop-loss (force-sell any position at -HARD_STOP_LOSS_PCT)
        2. Ask Claude for a trade decision via MCP
        3. Execute the decision if confidence clears the threshold
        4. Print dashboard
    """
    last_decision: dict | None = None
    logger.info(f"Real-time loop started (poll every {config.POLL_INTERVAL_SEC}s)")

    while True:
        cycle_start = datetime.now()
        logger.info(f"\n{'─' * 60}")
        logger.info(f"Poll cycle at {cycle_start.strftime('%H:%M:%S')}")

        # ── 1. Hard stop-loss check (independent of Claude) ──────────────────
        force_sold = trader.check_hard_stop_loss()
        if force_sold:
            logger.warning(f"Stop-loss triggered for: {force_sold}")

        # ── 2. Get Claude's decision ──────────────────────────────────────────
        try:
            decision = await mcp_client.get_trade_decision()
            last_decision = decision
        except Exception as e:
            logger.error(f"MCP client error: {e}  — defaulting to HOLD")
            decision = {
                "action": "HOLD",
                "instrument": None,
                "confidence": 0.0,
                "reasoning": f"MCP error: {e}",
            }

        action     = decision.get("action", "HOLD")
        instrument = decision.get("instrument")
        confidence = float(decision.get("confidence", 0.0))

        logger.info(
            f"Claude → {action}  instrument={instrument}  "
            f"confidence={confidence:.0%}"
        )
        logger.info(f"Reasoning: {decision.get('reasoning', '')[:200]}")

        # ── 3. Execute decision ───────────────────────────────────────────────
        if action == "BUY" and instrument and confidence >= config.MIN_CONFIDENCE_BUY:
            if config.DRY_RUN:
                logger.info(f"[DRY-RUN] Would BUY {instrument} — skipping real order")
            else:
                try:
                    from trader import load_state
                    state = load_state(config.STATE_PATH)
                    if instrument not in state:
                        # Fetch live price and buy
                        price = trader.get_live_price(instrument)
                        if price:
                            success = trader.place_buy_order(instrument, price)
                            if success:
                                from trader import save_state
                                state[instrument] = {
                                    "buy_price": round(price, 4),
                                    "quantity": max(
                                        1,
                                        int(
                                            (config.TOTAL_CORPUS / config.MAX_HOLDINGS)
                                            / price
                                        ),
                                    ),
                                    "buy_date": datetime.now().strftime("%Y-%m-%d"),
                                    "claude_reason": decision.get("reasoning", ""),
                                }
                                save_state(state, config.STATE_PATH)
                    else:
                        logger.info(f"BUY skipped — {instrument} already held")
                except Exception as e:
                    logger.error(f"BUY execution error: {e}")

        elif action == "SELL" and instrument and confidence >= config.MIN_CONFIDENCE_SELL:
            if config.DRY_RUN:
                logger.info(f"[DRY-RUN] Would SELL {instrument} — skipping real order")
            else:
                try:
                    from trader import load_state
                    state = load_state(config.STATE_PATH)
                    if instrument in state:
                        qty = state[instrument].get("quantity", 1)
                        trader.place_sell_order(instrument, qty)
                    else:
                        logger.info(f"SELL skipped — {instrument} not in portfolio")
                except Exception as e:
                    logger.error(f"SELL execution error: {e}")

        else:
            logger.info(
                f"No action taken — "
                f"action={action}  confidence={confidence:.0%}  "
                f"min_buy={config.MIN_CONFIDENCE_BUY:.0%}  "
                f"min_sell={config.MIN_CONFIDENCE_SELL:.0%}"
            )

        # ── 4. Dashboard ──────────────────────────────────────────────────────
        print_dashboard(trader, last_decision)

        # ── Sleep until next poll ─────────────────────────────────────────────
        elapsed = (datetime.now() - cycle_start).total_seconds()
        sleep_for = max(0, config.POLL_INTERVAL_SEC - elapsed)
        logger.info(f"Cycle took {elapsed:.1f}s  — sleeping {sleep_for:.0f}s")
        await asyncio.sleep(sleep_for)


# ─── Legacy daily schedule (original behaviour) ───────────────────────────────
def legacy_main():
    logger.info("AlgoTrader Bot starting in LEGACY (daily schedule) mode...")

    if not config.UPSTOX_ACCESS_TOKEN:
        logger.error("UPSTOX_ACCESS_TOKEN not set.")
        return

    trader = AlgoTrader(config)
    schedule.every().day.at(config.EXECUTION_TIME).do(trader.daily_execution)
    logger.info(f"Scheduled daily execution at {config.EXECUTION_TIME}")

    print_dashboard(trader)
    while True:
        schedule.run_pending()
        time.sleep(60)
        print_dashboard(trader)


# ─── Real-time main ────────────────────────────────────────────────────────────
async def realtime_main():
    logger.info("AlgoTrader Bot starting in REAL-TIME (Claude MCP) mode...")

    if not config.UPSTOX_ACCESS_TOKEN:
        logger.error("UPSTOX_ACCESS_TOKEN not set. Add it to .env")
        return

    if not config.ANTHROPIC_API_KEY:
        logger.error("ANTHROPIC_API_KEY not set. Add it to .env")
        return

    if config.DRY_RUN:
        logger.warning(
            "⚠️  DRY_RUN=True — all orders are simulated. "
            "Set DRY_RUN=false in .env to trade with real money."
        )

    trader     = AlgoTrader(config)
    mcp_client = MCPClient()

    logger.info(
        f"MCP server expected at: {config.MCP_SERVER_URL}\n"
        f"Make sure mcp_server.py is running before the bot starts."
    )

    await realtime_loop(trader, mcp_client)


# ─── Entry point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if "--legacy" in sys.argv:
        legacy_main()
    else:
        asyncio.run(realtime_main())