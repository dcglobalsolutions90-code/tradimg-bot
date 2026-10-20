"""Run once: python setup_keys.py -- prompts for your own Kalshi API key ID and .pem path."""
import env_setup
ok = env_setup.ensure_credentials(force=True)
print("Credentials set." if ok else "No credentials configured; the bot will run in dry-run mode.")
