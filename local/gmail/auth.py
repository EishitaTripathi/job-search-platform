"""Gmail API authentication — gmail.readonly scope ONLY.

Usage:
    service = get_gmail_service()
    messages = service.users().messages().list(userId="me", q="is:unread").execute()

Security rules:
- gmail.readonly scope only — verified at startup
- token.json and credentials.json are gitignored
- Credentials path from GMAIL_CREDENTIALS_PATH env var
"""

import os

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# SECURITY: readonly scope only — never modify/send/delete emails
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def get_gmail_service():
    """Build and return an authenticated Gmail API service.

    On first run: opens browser for OAuth consent flow.
    On subsequent runs: uses cached token.json.
    """
    creds = None
    token_path = os.environ.get("GMAIL_TOKEN_PATH", "credentials/token.json")
    creds_path = os.environ.get(
        "GMAIL_CREDENTIALS_PATH", "credentials/credentials.json"
    )

    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
            creds = flow.run_local_server(port=0)

        with open(token_path, "w") as f:
            f.write(creds.to_json())
        os.chmod(token_path, 0o600)

    # Verify scope at startup (security rule)
    if creds.scopes and set(creds.scopes) != set(SCOPES):
        raise PermissionError(
            f"Gmail token has unexpected scopes: {creds.scopes}. "
            f"Expected: {SCOPES}. Delete token.json and re-authenticate."
        )

    return build("gmail", "v1", credentials=creds)


def fetch_recent_emails(service, max_results: int = 500) -> list[dict]:
    """Fetch emails from the last 4 months, returning id/subject/snippet/body."""
    messages = []
    page_token = None

    while len(messages) < max_results:
        request = (
            service.users()
            .messages()
            .list(
                userId="me",
                q="newer_than:120d",
                maxResults=min(100, max_results - len(messages)),
                **({"pageToken": page_token} if page_token else {}),
            )
        )
        results = request.execute()
        messages.extend(results.get("messages", []))
        page_token = results.get("nextPageToken")
        if not page_token:
            break

    emails = []

    for msg_meta in messages:
        msg = (
            service.users()
            .messages()
            .get(
                userId="me",
                id=msg_meta["id"],
                format="full",
            )
            .execute()
        )

        headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
        body = _extract_body(msg["payload"])

        emails.append(
            {
                "email_id": msg["id"],
                "subject": headers.get("Subject", ""),
                "snippet": msg.get("snippet", ""),
                "body": body,
            }
        )

    return emails


def _strip_html(html: str) -> str:
    """Strip HTML tags to get plain text."""
    import re

    text = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL)
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</?p[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _extract_body(payload: dict) -> str:
    """Extract body from Gmail message payload (text/plain preferred, text/html fallback)."""
    import base64

    if payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode(
            "utf-8", errors="replace"
        )

    # First pass: look for text/plain
    for part in payload.get("parts", []):
        if part["mimeType"] == "text/plain" and part.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(part["body"]["data"]).decode(
                "utf-8", errors="replace"
            )
        if part.get("parts"):
            result = _extract_body(part)
            if result:
                return result

    # Second pass: fall back to text/html (calendar invites often only have HTML)
    for part in payload.get("parts", []):
        if part["mimeType"] == "text/html" and part.get("body", {}).get("data"):
            html = base64.urlsafe_b64decode(part["body"]["data"]).decode(
                "utf-8", errors="replace"
            )
            return _strip_html(html)
        if part.get("parts"):
            for sub in part["parts"]:
                if sub.get("mimeType") == "text/html" and sub.get("body", {}).get(
                    "data"
                ):
                    html = base64.urlsafe_b64decode(sub["body"]["data"]).decode(
                        "utf-8", errors="replace"
                    )
                    return _strip_html(html)

    return ""
