#!/usr/bin/env python3
"""Run once to set the UI login credentials. Writes .env in the same directory."""
import getpass
import stat
from pathlib import Path
from werkzeug.security import generate_password_hash

ENV_PATH = Path(__file__).parent / ".env"

print("=== Picster UI — auth setup ===\n")

username = input("Username [admin]: ").strip() or "admin"

while True:
    password = getpass.getpass("Password (min 12 chars): ")
    if len(password) < 12:
        print("  Password must be at least 12 characters.\n")
        continue
    confirm = getpass.getpass("Confirm password:        ")
    if password == confirm:
        break
    print("  Passwords do not match — try again.\n")

pw_hash = generate_password_hash(password, method="scrypt")

# Preserve any existing .env keys unrelated to auth
existing: dict = {}
if ENV_PATH.exists():
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            existing[k.strip()] = v.strip()

existing["UI_USERNAME"]      = username
existing["UI_PASSWORD_HASH"] = pw_hash

ENV_PATH.write_text(
    "\n".join(f"{k}={v}" for k, v in existing.items()) + "\n",
    encoding="utf-8",
)

# Owner-read-write only (best-effort; Windows ACLs differ from POSIX)
try:
    ENV_PATH.chmod(stat.S_IRUSR | stat.S_IWUSR)
except Exception:
    pass

print(f"\n.env written → {ENV_PATH}")
print("Restart ui.py for changes to take effect.")
