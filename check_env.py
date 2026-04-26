import os, sys
sys.path.insert(0, '.')
from dotenv import load_dotenv
load_dotenv()

tok  = os.getenv('UPSTOX_ACCESS_TOKEN', '')
ant  = os.getenv('ANTHROPIC_API_KEY', '')
gc   = os.getenv('GOOGLE_CREDENTIALS_PATH', '')
sp   = os.getenv('STATE_PATH', '')
lp   = os.getenv('LOG_PATH', '')

print('=== Credential & Path Check ===')
print(f'UPSTOX_ACCESS_TOKEN : {"SET (JWT)" if tok.startswith("eyJ") else "PLACEHOLDER - needs real token"}')
print(f'ANTHROPIC_API_KEY   : {"SET" if ant.startswith("sk-ant") else "PLACEHOLDER - needs real key"}')
print(f'GOOGLE_CREDS file   : {"EXISTS" if os.path.exists(gc) else "NOT FOUND"} ({gc})')
sp_dir = os.path.dirname(sp)
lp_dir = os.path.dirname(lp)
print(f'STATE_PATH dir      : {"EXISTS" if os.path.exists(sp_dir) else "NOT FOUND"} ({sp_dir})')
print(f'LOG_PATH dir        : {"EXISTS" if os.path.exists(lp_dir) else "NOT FOUND"} ({lp_dir})')
print()

missing = []
if not tok.startswith("eyJ"):
    missing.append("UPSTOX_ACCESS_TOKEN")
if not ant.startswith("sk-ant"):
    missing.append("ANTHROPIC_API_KEY")
if not os.path.exists(gc):
    missing.append("GOOGLE_CREDENTIALS_PATH (file not found)")
if not os.path.exists(sp_dir):
    missing.append(f"STATE_PATH directory ({sp_dir}) does not exist")
if not os.path.exists(lp_dir):
    missing.append(f"LOG_PATH directory ({lp_dir}) does not exist")

if missing:
    print("BLOCKED - Cannot start bot. Fix these first:")
    for m in missing:
        print(f"  - {m}")
    sys.exit(1)
else:
    print("All checks passed - ready to run!")
