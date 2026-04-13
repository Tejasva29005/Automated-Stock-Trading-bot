import schedule
import time
import logging
import config
from trader import AlgoTrader
from datetime import datetime
logging.basicConfig(
    filename=config.LOG_PATH,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
console = logging.StreamHandler()
console.setLevel(logging.INFO)
logging.getLogger("").addHandler(console)

logger = logging.getLogger(__name__)


def print_dashboard(trader: AlgoTrader):
    """Print a live portfolio dashboard every 60 seconds."""
    try:
        summary = trader.get_portfolio_summary()
        print(f"\n{'='*60}")
        print(f"  ALGOTRADER ETF BOT — LIVE DASHBOARD")
        print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'='*60}")
        print(f"  Holdings: {len(summary)}/{config.MAX_HOLDINGS} ETFs")
        print(f"  Next Run: {schedule.next_run()}")
        print(f"{'─'*60}")

        if summary:
            print(f"  {'ETF Code':<25} {'Buy ₹':>8} {'Live ₹':>8} {'P&L':>8}  {'Qty':>4}")
            print(f"  {'─'*25} {'─'*8} {'─'*8} {'─'*8}  {'─'*4}")
            for code, info in summary.items():
                buy   = info.get("buy_price", 0)
                live  = info.get("live_price") or 0
                pnl   = info.get("pnl_pct")
                qty   = info.get("quantity", 1)
                pnl_s = f"{pnl:+.1f}%" if pnl is not None else "  N/A"
                print(f"  {code:<25} {buy:>8.2f} {live:>8.2f} {pnl_s:>8}  {qty:>4}")
        else:
            print("  No holdings yet.")

        print(f"{'='*60}\n")

    except Exception as e:
        logger.warning(f"Dashboard error: {e}")


def main():
    logger.info("AlgoTrader Bot starting up...")

    if not config.UPSTOX_ACCESS_TOKEN:
        logger.error("UPSTOX_ACCESS_TOKEN not set. Set it as an environment variable.")
        return

    trader = AlgoTrader(config)
    schedule.every().day.at(config.EXECUTION_TIME).do(trader.daily_execution)
    logger.info(f"Scheduled daily execution at {config.EXECUTION_TIME}")

    print_dashboard(trader)

    while True:
        schedule.run_pending()
        time.sleep(60)
        print_dashboard(trader)


if __name__ == "__main__":
    main()