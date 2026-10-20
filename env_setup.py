"""Credential setup: loads .env, and if no API key is configured, asks for one (interactive terminals only).

Your key never leaves your machine. Nothing here is ever committed (.env and *.pem are in .gitignore).
Without a key the bot only runs in dry-run mode and cannot place orders.
"""
import os, sys
from pathlib import Path

D = Path(__file__).parent
ENV_FILE = D / ".env"


def _load_env_file():
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _have_key():
    kp = os.environ.get("KALSHI_KEY_PATH")
    return bool(os.environ.get("KALSHI_KEY_ID") and kp and Path(kp).expanduser().exists())


def ensure_credentials(force=False):
    _load_env_file()
    if _have_key() and not force:
        return True
    if not (sys.stdin and sys.stdin.isatty()):
        return False
    print("\nNo Kalshi API key configured. Create one in Kalshi: Account settings > API keys.")
    print("Kalshi shows a key ID and lets you download a private key (.pem) once. Enter them below.")
    print("(Leave the key ID blank to skip and run in dry-run mode.)\n")
    key_id = input("API key ID: ").strip()
    if not key_id:
        return False
    while True:
        key_path = input("Path to your private key .pem file: ").strip().strip('"')
        if Path(key_path).expanduser().exists():
            break
        print("File not found, try again (or Ctrl+C to cancel).")
    key_path = str(Path(key_path).expanduser().resolve())
    os.environ["KALSHI_KEY_ID"], os.environ["KALSHI_KEY_PATH"] = key_id, key_path
    if input("Save these to a local .env file (git-ignored) so you aren\'t asked again? [Y/n] ").strip().lower() != "n":
        lines = [l for l in (ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []) if not l.startswith(("KALSHI_KEY_ID=", "KALSHI_KEY_PATH="))]
        lines += [f"KALSHI_KEY_ID={key_id}", f"KALSHI_KEY_PATH={key_path}"]
        ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
        try:
            os.chmod(ENV_FILE, 0o600)
        except OSError:
            pass
        print(f"Saved to {ENV_FILE}")
    return True


if __name__ == "__main__":
    ok = ensure_credentials(force=True)
    print("Credentials set." if ok else "No credentials configured; the bot will run in dry-run mode.")
