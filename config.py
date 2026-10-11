import json
import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
DIGEST_RECIPIENT  = os.environ.get("DIGEST_RECIPIENT", "")
CREDENTIALS_FILE  = os.environ.get("CREDENTIALS_FILE", "credentials/credentials.json")
TOKEN_FILE        = os.environ.get("TOKEN_FILE", "credentials/token.json")

# On platforms like Railway, OAuth credentials are injected as env vars rather
# than mounted files; write them out so GmailClient can read them normally.
if os.environ.get("GOOGLE_CREDENTIALS"):
    os.makedirs(os.path.dirname(CREDENTIALS_FILE) or ".", exist_ok=True)
    with open(CREDENTIALS_FILE, "w") as f:
        json.dump(json.loads(os.environ["GOOGLE_CREDENTIALS"]), f)

if os.environ.get("GOOGLE_TOKEN"):
    os.makedirs(os.path.dirname(TOKEN_FILE) or ".", exist_ok=True)
    with open(TOKEN_FILE, "w") as f:
        json.dump(json.loads(os.environ["GOOGLE_TOKEN"]), f)

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]

DAYS_TO_SCAN          = int(os.environ.get("DAYS_TO_SCAN", "7"))
CLASSIFIER_BATCH_SIZE = 50
CLEANUP_LABEL_NAME = "gmail-cleanup"
BASE_URL           = os.environ.get("BASE_URL", "http://localhost:5000")


def _load_dashboard_token() -> str:
    if tok := os.environ.get("DASHBOARD_TOKEN"):
        return tok
    token_file = Path("data/token.txt")
    if token_file.exists():
        return token_file.read_text().strip()
    tok = secrets.token_urlsafe(32)
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(tok)
    return tok


DASHBOARD_TOKEN = _load_dashboard_token()
