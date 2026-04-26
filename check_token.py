import base64, json, time, os
from dotenv import load_dotenv
load_dotenv()

token = os.getenv("UPSTOX_ACCESS_TOKEN", "")
if not token:
    print("UPSTOX_ACCESS_TOKEN not set in .env")
    exit(1)

payload = token.split('.')[1]
payload += '=' * (4 - len(payload) % 4)
data = json.loads(base64.urlsafe_b64decode(payload))
exp = data.get('exp', 0)
iat = data.get('iat', 0)
now = int(time.time())
fmt = "%Y-%m-%d %H:%M:%S UTC"
print("Token issued at :", time.strftime(fmt, time.gmtime(iat)))
print("Token expires at:", time.strftime(fmt, time.gmtime(exp)))
print("Current time    :", time.strftime(fmt, time.gmtime(now)))
if now < exp:
    days_left = (exp - now) // 86400
    print(f"Status          : VALID  ({days_left} days remaining)")
else:
    print("Status          : EXPIRED", str((now - exp)//3600) + "h ago")
