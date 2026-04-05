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


def fetch_recent_emails(service, max_results: int = 100) -> list[dict]:
    """Fetch emails from the last 4 months, returning id/subject/snippet/body."""
    results = (
        service.users()
        .messages()
        .list(
            userId="me",
            q="newer_than:120d",
            maxResults=max_results,
        )
        .execute()
    )

    messages = results.get("messages", [])
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


def _extract_body(payload: dict) -> str:
    """Extract plain text body from Gmail message payload."""
    import base64

    if payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode(
            "utf-8", errors="replace"
        )

    for part in payload.get("parts", []):
        if part["mimeType"] == "text/plain" and part.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(part["body"]["data"]).decode(
                "utf-8", errors="replace"
            )
        # Recurse into multipart
        if part.get("parts"):
            result = _extract_body(part)
            if result:
                return result

    return ""
