import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
DIGEST_RECIPIENT  = os.environ.get("DIGEST_RECIPIENT", "")
CREDENTIALS_FILE  = os.environ.get("CREDENTIALS_FILE", "credentials/credentials.json")
TOKEN_FILE        = os.environ.get("TOKEN_FILE", "credentials/token.json")
PENDING_FILE      = Path(os.environ.get("PENDING_FILE", "pending_deletions.json"))

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]

DAYS_TO_SCAN          = int(os.environ.get("DAYS_TO_SCAN", "7"))
CLASSIFIER_BATCH_SIZE = 50
STATS_FILE            = Path(os.environ.get("STATS_FILE", "data/stats.json"))
