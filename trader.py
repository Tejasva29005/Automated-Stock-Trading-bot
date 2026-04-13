import os
import json
import logging
import math
import upstox_client
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime

logger = logging.getLogger(__name__)
def load_state(path: str) -> dict:
    """Load portfolio state from JSON file."""
    if not os.path.exists(path):
        return {}
    with open(path, "r") as f:
        return json.load(f)


def save_state(state: dict, path: str):
    """Persist portfolio state to JSON file."""
    with open(path, "w") as f:
        json.dump(state, f, indent=2)
    logger.info(f"State saved → {path}  ({len(state)} holdings)")
def load_last_etf_list(path: str) -> list:
    if not os.path.exists(path):
        return []
    with open(path, "r") as f:
        return [line.strip() for line in f if line.strip()]


def save_last_etf_list(etf_codes: list, path: str):
    with open(path, "w") as f:
        for code in etf_codes:
            f.write(f"{code}\n")
class AlgoTrader:

    def __init__(self, config):
        self.config = config
        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = ServiceAccountCredentials.from_json_keyfile_name(
            config.GOOGLE_CREDENTIALS_PATH, scope
        )
        gc = gspread.authorize(creds)
        self.worksheet = gc.open_by_key(config.SPREADSHEET_ID).get_worksheet(0)
        cfg = upstox_client.Configuration()
        cfg.access_token = config.UPSTOX_ACCESS_TOKEN
        api_client = upstox_client.ApiClient(cfg)
        self.order_api  = upstox_client.OrderApi(api_client)
        self.market_api = upstox_client.MarketQuoteApi(api_client)
        self.state_path       = config.STATE_PATH
        self.last_list_path   = config.LAST_ETF_LIST_PATH
        self.total_corpus     = config.TOTAL_CORPUS     
        self.max_holdings     = config.MAX_HOLDINGS        
        self.profit_target    = config.PROFIT_TARGET       
        self.reentry_drop     = config.REENTRY_DROP        
        self.top_n            = config.TOP_N               

    def fetch_sheet_data(self) -> tuple[list, list]:
        etf_codes = self.worksheet.col_values(1)[1:31]
        prices_raw = self.worksheet.col_values(3)[1:31]
        prices = []
        for p in prices_raw:
            try:
                prices.append(float(str(p).replace(",", "").strip()))
            except (ValueError, TypeError):
                prices.append(None)
        while len(prices) < len(etf_codes):
            prices.append(None)

        return etf_codes, prices
    def get_live_price(self, instrument_token: str) -> float | None:
        try:
            quote = self.market_api.ltp(instrument_token, "2.0")
            return float(quote.data[instrument_token].ltp)
        except Exception as e:
            logger.warning(f"Could not fetch LTP for {instrument_token}: {e}")
            return None
    def place_buy_order(self, instrument_token: str, price: float) -> bool:
        budget   = self.total_corpus / self.max_holdings
        quantity = math.floor(budget / price) if price and price > 0 else 1
        if quantity < 1:
            quantity = 1

        try:
            req = upstox_client.PlaceOrderRequest(
                quantity=quantity,
                product="D",          
                validity="DAY",
                price=0,               
                instrument_token=instrument_token,
                order_type="MARKET",
                transaction_type="BUY",
            )
            resp = self.order_api.place_order(req, "2.0")
            logger.info(f"BUY  {instrument_token}  qty={quantity}  "
                        f"~₹{price:.2f}  order_id={resp.data.order_id}")
            return True
        except Exception as e:
            logger.error(f"BUY order failed for {instrument_token}: {e}")
            return False

    def place_sell_order(self, instrument_token: str, quantity: int) -> bool:
        try:
            req = upstox_client.PlaceOrderRequest(
                quantity=quantity,
                product="D",
                validity="DAY",
                price=0,
                instrument_token=instrument_token,
                order_type="MARKET",
                transaction_type="SELL",
            )
            resp = self.order_api.place_order(req, "2.0")
            logger.info(f"SELL {instrument_token}  qty={quantity}  "
                        f"order_id={resp.data.order_id}")
            return True
        except Exception as e:
            logger.error(f"SELL order failed for {instrument_token}: {e}")
            return False
    def daily_execution(self):
        logger.info("=" * 60)
        logger.info(f"DAILY EXECUTION  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        etf_codes, sheet_prices = self.fetch_sheet_data()
        top_n_codes  = [c.strip() for c in etf_codes[:self.top_n] if c.strip()]
        top_30_codes = [c.strip() for c in etf_codes if c.strip()]

        if not top_n_codes:
            logger.warning("Sheet returned no ETF codes. Aborting.")
            return

        today_rank1 = top_n_codes[0]
        logger.info(f"Today's #1 ETF: {today_rank1}")
        logger.info(f"Top {self.top_n}: {top_n_codes}")
        state         = load_state(self.state_path)          # { code: {buy_price, qty, date} }
        last_etf_list = load_last_etf_list(self.last_list_path)
        yesterday_rank1 = last_etf_list[0] if last_etf_list else None

        logger.info(f"Yesterday's #1 ETF: {yesterday_rank1}")
        logger.info(f"Current holdings ({len(state)}): {list(state.keys())}")
        self._check_and_sell(state)
        state = load_state(self.state_path)
        self._check_reentry(state)
        state = load_state(self.state_path)
        if len(state) >= self.max_holdings:
            logger.info(f"Portfolio full ({len(state)}/{self.max_holdings}). No new buy.")
        else:
            self._execute_buy(
                top_n_codes,
                sheet_prices,
                state,
                today_rank1,
                yesterday_rank1,
            )
        save_last_etf_list(top_n_codes, self.last_list_path)
        state = load_state(self.state_path)
        logger.info(f"End of day holdings ({len(state)}): {list(state.keys())}")
        logger.info("=" * 60)
    def _check_and_sell(self, state: dict):
        if not state:
            return

        to_sell = []
        for code, info in state.items():
            buy_price = info.get("buy_price")
            quantity  = info.get("quantity", 1)
            if not buy_price:
                continue

            live_price = self.get_live_price(code)
            if live_price is None:
                continue

            profit_pct = (live_price - buy_price) / buy_price
            logger.info(f"  {code}: buy=₹{buy_price:.2f}  live=₹{live_price:.2f}  "
                        f"P&L={profit_pct*100:.1f}%")

            if profit_pct >= self.profit_target:
                logger.info(f"  → SELL SIGNAL: {code} hit {profit_pct*100:.1f}% profit")
                to_sell.append((code, quantity, live_price))

        for code, quantity, live_price in to_sell:
            success = self.place_sell_order(code, quantity)
            if success:
                del state[code]
                save_state(state, self.state_path)
                logger.info(f"  → Sold {code} at ₹{live_price:.2f}")

    def _check_reentry(self, state: dict):
        if not state:
            return

        for code, info in list(state.items()):
            buy_price = info.get("buy_price")
            if not buy_price:
                continue

            live_price = self.get_live_price(code)
            if live_price is None:
                continue

            drop_pct = (buy_price - live_price) / buy_price
            if drop_pct >= self.reentry_drop:
                logger.info(f"  RE-ENTRY: {code} dropped {drop_pct*100:.1f}% "
                            f"(buy=₹{buy_price:.2f} now=₹{live_price:.2f})")
                success = self.place_buy_order(code, live_price)
                if success:
                    old_qty = info.get("quantity", 1)
                    budget  = self.total_corpus / self.max_holdings
                    new_qty = math.floor(budget / live_price) if live_price > 0 else 1
                    total_qty  = old_qty + new_qty
                    avg_price  = (buy_price * old_qty + live_price * new_qty) / total_qty
                    state[code]["buy_price"] = round(avg_price, 4)
                    state[code]["quantity"]  = total_qty
                    save_state(state, self.state_path)
                    logger.info(f"  → Re-entered {code}: avg_price=₹{avg_price:.2f}  "
                                f"total_qty={total_qty}")
    def _execute_buy(
        self,
        top_n_codes: list,
        sheet_prices: list,
        state: dict,
        today_rank1: str,
        yesterday_rank1: str | None,
    ):
        rank1_changed = (today_rank1 != yesterday_rank1)

        if rank1_changed:
            logger.info(f"#1 changed ({yesterday_rank1} → {today_rank1}). "
                        "Targeting #1 for buy.")
            candidate_codes = top_n_codes          # Start from #1
        else:
            logger.info(f"#1 unchanged ({today_rank1}). Cascading down the list.")
            # Skip #1, start from #2
            candidate_codes = top_n_codes[1:] if len(top_n_codes) > 1 else top_n_codes

        bought_set = set(state.keys())

        for i, code in enumerate(candidate_codes):
            if code in bought_set:
                logger.info(f"  Skipping {code} — already held")
                continue
            live_price = self.get_live_price(code)
            sheet_idx  = top_n_codes.index(code) if code in top_n_codes else -1
            sheet_price = sheet_prices[sheet_idx] if sheet_idx >= 0 else None
            buy_price  = live_price or sheet_price

            if not buy_price:
                logger.warning(f"  No price for {code}, skipping.")
                continue

            logger.info(f"  Buying: {code} at ₹{buy_price:.2f} "
                        f"(rank {top_n_codes.index(code)+1})")

            budget   = self.total_corpus / self.max_holdings
            quantity = math.floor(budget / buy_price) if buy_price > 0 else 1
            quantity = max(1, quantity)

            success = self.place_buy_order(code, buy_price)
            if success:
                state[code] = {
                    "buy_price": round(buy_price, 4),
                    "quantity":  quantity,
                    "buy_date":  datetime.now().strftime("%Y-%m-%d"),
                }
                save_state(state, self.state_path)
                logger.info(f"  ✓ Bought {code}  qty={quantity}  ₹{buy_price:.2f}")
            return  

        logger.info("No eligible ETF found to buy today.")
    def get_portfolio_summary(self) -> dict:
        state = load_state(self.state_path)
        summary = {}
        for code, info in state.items():
            live = self.get_live_price(code)
            buy  = info.get("buy_price", 0)
            qty  = info.get("quantity", 1)
            pnl  = round((live - buy) / buy * 100, 2) if live and buy else None
            summary[code] = {
                **info,
                "live_price": live,
                "pnl_pct":    pnl,
            }
        return summary