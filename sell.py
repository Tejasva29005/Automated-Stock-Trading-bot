import gspread
from oauth2client.service_account import ServiceAccountCredentials
import numpy as np
import os
import upstox_client
from upstox_client.rest import ApiException

UPSTOX_ACCESS_TOKEN = "eyJ0eXAiOiJKV1QiLCJrZXlfaWQiOiJza192MS4wIiwiYWxnIjoiSFMyNTYifQ.eyJzdWIiOiIzWEM1TDMiLCJqdGkiOiI2OWI0NTI0NTZjM2U1OTM4ODc4OTI5MjAiLCJpc011bHRpQ2xpZW50IjpmYWxzZSwiaXNQbHVzUGxhbiI6ZmFsc2UsImlhdCI6MTc3MzQyNTIyMSwiaXNzIjoidWRhcGktZ2F0ZXdheS1zZXJ2aWNlIiwiZXhwIjoxNzczNDM5MjAwfQ.zD3L_xFJ7Q80LoUzKhDv1sqr3pg6TOErKkjsX5AhNeI"  # Replace with your token

configuration = upstox_client.Configuration()
configuration.access_token = UPSTOX_ACCESS_TOKEN
api_client = upstox_client.ApiClient(configuration)
order_api = upstox_client.OrderApi(api_client)
market_api = upstox_client.MarketQuoteApi(api_client)

GOOGLE_CREDENTIALS_PATH = r"C:\stocks\proven-dialect-464102-v2-0384517aa543.json"
SPREADSHEET_ID = "11jiulCHWABq9RitHBxbQDDunR-468DG9bq6apH7su8g"
STATE_PATH = r"C:\stocks\buy.txt"

scope = [
    'https://spreadsheets.google.com/feeds',
    'https://www.googleapis.com/auth/drive'
]
creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDENTIALS_PATH, scope)
gc = gspread.authorize(creds)
spreadsheet = gc.open_by_key(SPREADSHEET_ID)
worksheet = spreadsheet.get_worksheet(0)

def load_bought(filename=STATE_PATH):
    if not os.path.exists(filename):
        return set()
    with open(filename, "r") as f:
        return set(line.strip() for line in f if line.strip())

def save_bought(bought, filename=STATE_PATH):
    with open(filename, "w") as f:
        for code in bought:
            f.write(f"{code}\n")

print("Starting daily execution...")

bought = load_bought()
etf_codes = worksheet.col_values(1)[1:31]  
prices = worksheet.col_values(3)[1:31]     

print(f"Current holdings: {len(bought)} ETFs")
print(f"Today's Top 13 ETFs: {len([c for c in etf_codes[:13] if c])} available")

for i in range(min(13, len(etf_codes))):
    code = etf_codes[i].strip() if etf_codes[i] else ""
    if code and code in bought:
        print(f"Selling ETF: {code}")
        
        try:
            quote = market_api.ltp(code, "2.0")
            current_price = quote.data[code].ltp
            
            quantity = 1  
            order_req = upstox_client.PlaceOrderRequest(
                quantity=quantity,
                product="D",  
                validity="DAY",
                price=0,  
                instrument_token=code,
                order_type="MARKET",
                transaction_type="SELL"
            )
            response = order_api.place_order(order_req, "2.0")
            print(f"SELL ORDER PLACED: {response.data.order_id}")
            
        except Exception as e:
            print(f"Sell order failed: {e}")
        
        bought.remove(code)
        save_bought(bought)
        print("Execution complete.")
        exit()
