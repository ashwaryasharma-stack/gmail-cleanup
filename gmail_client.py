from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from config import CREDENTIALS_FILE, CLEANUP_LABEL_NAME, GMAIL_SCOPES, TOKEN_FILE

# System labels that don't protect an email from deletion
_SYSTEM_LABELS = {
    "INBOX", "DRAFT", "SENT", "TRASH", "SPAM", "UNREAD", "STARRED",
    "IMPORTANT", "ALL_MAIL", "CATEGORY_PERSONAL", "CATEGORY_SOCIAL",
    "CATEGORY_PROMOTIONS", "CATEGORY_UPDATES", "CATEGORY_FORUMS",
}


class GmailClient:
    def __init__(self):
        self.service = self._authenticate()
        self._user_email: str | None = None
        self._cleanup_label_id: str | None = None

    def _authenticate(self):
        creds_path = Path(CREDENTIALS_FILE)
        token_path = Path(TOKEN_FILE)

        if not creds_path.exists():
            raise FileNotFoundError(
                f"OAuth credentials not found at '{CREDENTIALS_FILE}'.\n"
                "Download them from Google Cloud Console > APIs & Services > Credentials\n"
                "and save as credentials/credentials.json."
            )

        creds = None
        if token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), GMAIL_SCOPES)
                creds = flow.run_local_server(port=0)
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.write_text(creds.to_json())

        return build("gmail", "v1", credentials=creds)

    @property
    def user_email(self) -> str:
        if not self._user_email:
            profile = self.service.users().getProfile(userId="me").execute()
            self._user_email = profile["emailAddress"]
        return self._user_email

    def _get_or_create_cleanup_label(self) -> str:
        if self._cleanup_label_id:
            return self._cleanup_label_id
        result = self.service.users().labels().list(userId="me").execute()
        for label in result.get("labels", []):
            if label["name"] == CLEANUP_LABEL_NAME:
                self._cleanup_label_id = label["id"]
                return self._cleanup_label_id
        created = self.service.users().labels().create(
            userId="me",
            body={
                "name": CLEANUP_LABEL_NAME,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
                "color": {"backgroundColor": "#ffd6a5", "textColor": "#4a1f00"},
            },
        ).execute()
        self._cleanup_label_id = created["id"]
        return self._cleanup_label_id

    def fetch_recent_emails(self, days: int = 7) -> list[dict]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        # Fetch from Primary inbox only, exclude Gmail's system categories
        query = f"after:{cutoff.strftime('%Y/%m/%d')} category:primary -category:promotions -category:spam"

        message_ids: list[str] = []
        page_token = None
        while True:
            kwargs: dict = {"userId": "me", "q": query, "maxResults": 500}
            if page_token:
                kwargs["pageToken"] = page_token
            result = self.service.users().messages().list(**kwargs).execute()
            message_ids.extend(m["id"] for m in result.get("messages", []))
            page_token = result.get("nextPageToken")
            if not page_token:
                break

        emails = []
        for msg_id in message_ids:
            detail = self.service.users().messages().get(
                userId="me",
                id=msg_id,
                format="metadata",
                metadataHeaders=["From", "Subject", "Date", "In-Reply-To"],
            ).execute()
            headers = {h["name"]: h["value"] for h in detail["payload"]["headers"]}
            label_ids = detail.get("labelIds", [])
            has_in_reply_to = "In-Reply-To" in headers

            emails.append({
                "id": msg_id,
                "subject": headers.get("Subject", "(no subject)"),
                "sender": headers.get("From", "unknown"),
                "date": headers.get("Date", ""),
                "snippet": detail.get("snippet", ""),
                "label_ids": label_ids,
                "has_been_replied_to": has_in_reply_to,
            })

        return emails

    def archive_emails(self, email_ids: list[str]) -> int:
        label_id = self._get_or_create_cleanup_label()
        count = 0
        for email_id in email_ids:
            self.service.users().messages().modify(
                userId="me",
                id=email_id,
                body={"addLabelIds": [label_id], "removeLabelIds": ["INBOX"]},
            ).execute()
            count += 1
        return count

    def is_email_protected(self, email: dict) -> bool:
        """Returns True if email has custom labels or has been replied to."""
        # Check if email has been replied to
        if email.get("has_been_replied_to"):
            return True

        # Check if email has custom labels (non-system labels)
        label_ids = email.get("label_ids", [])
        for label_id in label_ids:
            if label_id not in _SYSTEM_LABELS:
                return True

        return False

    def send_digest_email(self, recipient: str, subject: str, html_body: str) -> None:
        message = MIMEMultipart("alternative")
        message["Subject"] = subject
        message["From"] = self.user_email
        message["To"] = recipient
        message.attach(MIMEText(html_body, "html"))

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        self.service.users().messages().send(
            userId="me", body={"raw": raw}
        ).execute()
