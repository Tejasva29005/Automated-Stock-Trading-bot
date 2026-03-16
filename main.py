import schedule
import time
import logging
import config
from trader import AlgoTrader
from datetime import datetime

logging.basicConfig(
    filename='C:\\stocks\\trader.log',
    level=logging.INFO,
    format='%(asctime)s - %(message)s'
)

def print_dashboard(trader):
    bought = trader.load_bought()
    print(f"\n{'='*50}")
    print(f"ALGOTRADER ETF BOT - LIVE DASHBOARD")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Holdings: {len(bought)} ETFs")
    print(f"Next Run: {schedule.next_run()}")
    print(f"State File: {config.STATE_PATH}")
    print(f"{'='*50}\n")

def main():
    trader = AlgoTrader(config)
    
    schedule.every().day.at("15:00").do(lambda: trader.daily_execution())
    
    print("AlgoTrader Dashboard Started!")
    print_dashboard(trader)
    
    while True:
        schedule.run_pending()
        time.sleep(60)
        print_dashboard(trader)

if __name__ == "__main__":
    main()
