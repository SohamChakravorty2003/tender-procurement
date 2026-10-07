import base64
import re
import threading
from html.parser import HTMLParser
from pathlib import Path
from app.classify_service import analyze_email_cached
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from app.bid_service import register_bid_if_applicable
from app.tender_store import is_email_deleted


SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

CLIENT_SECRET_FILE = Path("credentials/client_secret.json")
TOKEN_FILE = Path("credentials/token.json")

# The inbox polls repeatedly. Keep the last fetched message details in memory so
# a refresh lists the inbox but only downloads details for newly seen messages.
_EMAIL_CACHE: dict[str, dict] = {}
_EMAIL_CACHE_LOCK = threading.Lock()


def authenticate():
    """
    Authenticate the user with Google OAuth.

    If token.json already exists, use the saved credentials.
    If the credentials have expired, refresh them.
    If no credentials exist, start the OAuth flow.
    """

    credentials = None

    if TOKEN_FILE.exists():
        credentials = Credentials.from_authorized_user_file(
            TOKEN_FILE,
            SCOPES
        )

    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())

        TOKEN_FILE.write_text(
            credentials.to_json()
        )

    if credentials is None:
        flow = InstalledAppFlow.from_client_secrets_file(
            CLIENT_SECRET_FILE,
            SCOPES
        )

        credentials = flow.run_local_server(
            port=0
        )

        TOKEN_FILE.write_text(
            credentials.to_json()
        )

    return credentials


def get_gmail_service():
    """
    Create and return an authenticated Gmail API service.
    """

    credentials = authenticate()

    service = build(
        "gmail",
        "v1",
        credentials=credentials
    )

    return service


def decode_body(data):
    """
    Decode Gmail's Base64URL encoded email body.
    """

    data += "=" * (-len(data) % 4)
    decoded_bytes = base64.urlsafe_b64decode(data)

    return decoded_bytes.decode(
        "utf-8",
        errors="replace"
    )


def _find_part(payload, mime_type):
    """
    Recursively search Gmail's MIME structure for a part matching
    mime_type and return its decoded content, or None if not found.

    This is the same recursive search extract_body always did — pulled
    out as its own function so it can be reused for both text/plain and
    text/html without duplicating the traversal logic.
    """

    if payload.get("mimeType") == mime_type:

        body_data = payload.get("body", {}).get("data")

        if body_data:
            return decode_body(body_data)

    for part in payload.get("parts", []):

        result = _find_part(part, mime_type)

        if result:
            return result

    return None


class _HTMLTextExtractor(HTMLParser):
    """
    Minimal HTML-to-text converter, used only as a fallback when an email
    has no text/plain part. Strips tags and skips <script>/<style>
    content. Not meant to preserve formatting or layout — the goal is
    giving the LLM readable words instead of nothing, not rendering
    the email.
    """

    def __init__(self):
        super().__init__()
        self._chunks = []
        self._skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip = True

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = False

    def handle_data(self, data):
        if not self._skip:
            self._chunks.append(data)

    def get_text(self):
        text = " ".join(self._chunks)
        return re.sub(r"\s+", " ", text).strip()


def html_to_text(html: str) -> str:
    parser = _HTMLTextExtractor()
    parser.feed(html)
    return parser.get_text()


def extract_body(payload):
    """
    Extract readable text from a Gmail message payload.

    Prefers the text/plain part, since it's already clean text. Falls
    back to text/html (converted to plain text) when no text/plain part
    exists anywhere in the message — common for marketing emails,
    receipts, and vendor mail clients that only send an HTML body.
    Returns "" only if neither part type is present at all.
    """

    plain = _find_part(payload, "text/plain")
    if plain:
        return plain

    html = _find_part(payload, "text/html")
    if html:
        return html_to_text(html)

    return ""

def extract_attachment_metadata(payload):
    """Return attachment filename, MIME type, Gmail attachment id and reported size."""
    attachments = []
    body = payload.get("body") or {}
    filename = (payload.get("filename") or "").strip()
    attachment_id = body.get("attachmentId")
    if filename or attachment_id:
        attachments.append({
            "filename": filename or f"attachment-{attachment_id}",
            "mime_type": (payload.get("mimeType") or "application/octet-stream").lower(),
            "attachment_id": attachment_id,
            "size": body.get("size"),
        })
    for part in payload.get("parts", []):
        attachments.extend(extract_attachment_metadata(part))
    return attachments


def get_header(headers, header_name):
    """
    Find a specific header such as From, To, Subject, or Date.
    """

    for header in headers:

        if header.get("name", "").lower() == header_name.lower():
            return header.get("value", "")

    return ""


def get_emails(max_results=10):
    """
    Fetch emails from the authenticated Gmail inbox
    and return them in a clean structure. Reuse full message data already
    fetched during this process so inbox polling does not re-download 50 bodies.
    """

    with _EMAIL_CACHE_LOCK:
        return _get_emails(max_results)


def _get_emails(max_results: int) -> list[dict]:
    """Fetch the current inbox IDs and hydrate only IDs absent from the cache."""

    service = get_gmail_service()

    results = service.users().messages().list(
        userId="me",
        maxResults=max_results,
        labelIds=["INBOX"]
    ).execute()

    messages = results.get("messages", [])

    emails = []
    current_ids = {message["id"] for message in messages}

    for message in messages:

        message_id = message["id"]

        cached = _EMAIL_CACHE.get(message_id)
        if cached:
            email = dict(cached["email"])
            analysis = analyze_email_cached(
                email_id=message_id,
                thread_id=email.get("thread_id"),
                subject=email.get("subject") or "",
                body=email.get("analysis_body") or "",
                sender=email.get("sender"),
            )
            email["category"] = analysis["category"] or "other"
            email["tender_id"] = analysis["tender_id"]
            email["email_role"] = analysis["email_role"]
            email["analysis_deleted"] = is_email_deleted(message_id)
            register_bid_if_applicable(email, cached["attachment_names"])
            emails.append(email)
            continue

        full_message = service.users().messages().get(
            userId="me",
            id=message_id,
            format="full"
        ).execute()

        payload = full_message.get("payload", {})

        headers = payload.get("headers", [])

        sender = get_header(headers, "From")
        recipient = get_header(headers, "To")
        subject = get_header(headers, "Subject")
        date = get_header(headers, "Date")

        body = extract_body(payload)
        analysis_body = _strip_quoted(body)
        attachment_names = [a["filename"] for a in extract_attachment_metadata(payload) if a["filename"]]

        analysis = analyze_email_cached(
            email_id=message_id,
            thread_id=full_message.get("threadId"),
            subject=subject,
            body=analysis_body,
            sender = sender

        )

        email = {
            "id": full_message.get("id"),
            "thread_id": full_message.get("threadId"),
            "sender": sender,
            "recipient": recipient,
            "subject": subject,
            "date": date,
            "body": body,
            "snippet": full_message.get("snippet", ""),
            "category": analysis["category"] or "other",
            "tender_id": analysis["tender_id"],
            "email_role": analysis["email_role"]
        }
        email["analysis_body"] = analysis_body
        email["analysis_deleted"] = is_email_deleted(message_id)
        register_bid_if_applicable(email, attachment_names) 
        emails.append(email)
        _EMAIL_CACHE[message_id] = {"email": dict(email), "attachment_names": attachment_names}

    # Inbox IDs are bounded by max_results; forget details that are no longer in
    # the visible inbox instead of growing this process cache without limit.
    for message_id in tuple(_EMAIL_CACHE):
        if message_id not in current_ids:
            del _EMAIL_CACHE[message_id]

    return emails


def get_email_by_id_for_offers(message_id: str) -> tuple[dict, list[str]]:
    """Fetch one known Gmail message by ID, reusing inbox details when available.

    Does not list the inbox or run classification. Gmail errors propagate to the
    caller so the tender can report a retryable failure.
    """
    with _EMAIL_CACHE_LOCK:
        cached = _EMAIL_CACHE.get(message_id)
        if cached:
            return dict(cached["email"]), list(cached["attachment_names"])

        full_message = get_gmail_service().users().messages().get(
            userId="me", id=message_id, format="full"
        ).execute()
        if full_message.get("id") != message_id:
            raise ValueError("Gmail returned a different message ID")
        payload = full_message.get("payload") or {}
        headers = payload.get("headers") or []
        body = extract_body(payload)
        email = {
            "id": message_id,
            "thread_id": full_message.get("threadId"),
            "sender": get_header(headers, "From"),
            "recipient": get_header(headers, "To"),
            "subject": get_header(headers, "Subject"),
            "date": get_header(headers, "Date"),
            "body": body,
            "analysis_body": _strip_quoted(body),
            "snippet": full_message.get("snippet", ""),
        }
        attachment_names = [
            item["filename"] for item in extract_attachment_metadata(payload)
            if item["filename"]
        ]
        _EMAIL_CACHE[message_id] = {
            "email": dict(email), "attachment_names": attachment_names,
        }
        return email, attachment_names

_QUOTE_HEADER = re.compile(r"^\s*On\s.+|.*wrote:\s*$", re.I)
_OUTLOOK_QUOTE_START = re.compile(
    r"^\s*(?:-{2,}\s*Original Message\s*-{2,}|From:\s*.+)\s*$", re.I
)
_OUTLOOK_QUOTE_HEADER = re.compile(r"^\s*(?:Sent:|To:|Cc:|Subject:)\s*.+$", re.I)


def _strip_quoted(text: str) -> str:
    """Remove common Gmail and Outlook quoted blocks from a reply body."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.match(r"^\s*On\s.+", line, re.I):
            for j in range(i, min(i + 5, len(lines))):
                if re.search(r"\bwrote:\s*$", lines[j], re.I):
                    return "\n".join(lines[:i]).strip()
            if line.rstrip().endswith("<") and not any(item.strip() for item in lines[i + 1 :]):
                return "\n".join(lines[:i]).strip()

        if line.lstrip().startswith(">"):
            cut = i
            # Keep a short typed reply between a quote header and the quote itself.
            for j in range(i - 1, max(i - 4, -1), -1):
                if _QUOTE_HEADER.match(lines[j]):
                    cut = j
                    break
            return "\n".join(lines[:cut]).strip()

        if _OUTLOOK_QUOTE_START.match(line):
            return "\n".join(lines[:i]).strip()

        if line.strip().lower() == "from:" and i + 1 < len(lines):
            following = lines[i + 1 : i + 7]
            header_count = sum(bool(_OUTLOOK_QUOTE_HEADER.match(item)) for item in following)
            if header_count >= 2:
                return "\n".join(lines[:i]).strip()

    return text.strip()


def get_thread(thread_id: str) -> list[dict]:
    """All messages of one Gmail thread, oldest first, quoted history removed.
    Read-only: nothing is analysed or stored."""
    service = get_gmail_service()
    thread = service.users().threads().get(
        userId="me", id=thread_id, format="full"
    ).execute()

    messages = []
    for m in thread.get("messages", []):
        payload = m.get("payload", {})
        headers = payload.get("headers", [])
        messages.append({
            "id": m.get("id"),
            "sender": get_header(headers, "From"),
            "recipient": get_header(headers, "To"),
            "subject": get_header(headers, "Subject"),
            "date": get_header(headers, "Date"),
            "body": _strip_quoted(extract_body(payload)),
            "attachments": [a["filename"] for a in extract_attachment_metadata(payload) if a["filename"]],
            "sent_by_me": "SENT" in m.get("labelIds", []),
        })
    return messages


if __name__ == "__main__":

    emails = get_emails(
        max_results=4
    )

    print(f"\nFound {len(emails)} emails\n")

    print("=" * 70)

    for email in emails:

        print(f"ID       : {email['id']}")
        print(f"From     : {email['sender']}")
        print(f"To       : {email['recipient']}")
        print(f"Subject  : {email['subject']}")
        print(f"Date     : {email['date']}")
        print(f"Snippet  : {email['snippet']}")

        print("\nBody:")
        print(email["body"])
        print("--------------------------------------------------")
        print(f"Category : {email['category']}")
        print(f"Tender ID: {email['tender_id']}")
        print(f"Role     : {email['email_role']}")

        print("\n" + "=" * 70)
