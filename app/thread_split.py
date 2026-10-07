"""
Splits an email-thread printout into its messages, using the header block that
precedes each one (no LLM). The text between two headers is that message's own
new text, because earlier messages are printed below the later ones.

Header shapes recognised:
  1. "From: X" followed within 5 lines by "Sent: ..." / "Date: ..."
  2. "On|At <date> <time>, X wrote:"
  3. Chinese-client block: a "label: sender" line followed by a "label: ...date..." line
     (matched by shape, not by the label characters)
  4. Gmail print style: a line ending in <address> followed by "Tue, Apr 2, 2024 at 8:24 PM"
Printer page headers/footers are removed. A short text before the first header
(subject line, "1 message") is dropped; a longer one is kept as a message with sender None.

Check the split of one stored thread (no database write, no LLM):
    uv run python -m app.thread_split --id 9
"""

import argparse
import re

_NON_ASCII = "[^\\x00-\\x7F]"
_PAGE_MARK = re.compile(r"^--- page \d+ ---$")
_PRINT_NOISE = re.compile(
    r"^\s*(?:\d{1,2}/\d{1,2}/\d{2,4},\s*\d{1,2}:\d{2}\s*[AP]M|https?://mail\.google\.com\S*|\d+/\d+)\b.*$",
    re.I,)
_PRINT_TITLE = re.compile(r"^\s*Minchem Impex India Private Limited Mail\s*-", re.I)
_FROM = re.compile(r"^\s*From:\s*(?P<who>.+?)\s*$", re.I)
_SENT = re.compile(r"^\s*(?:Sent|Date):\s*(?P<when>.+?)\s*$", re.I)
_ON_WROTE = re.compile(
    r"^\s*(?:On|At)\s+(?P<when>.+?\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AP]M)?),?\s+(?P<who>.+?)\s+wrote:\s*$",
    re.I,
)
_CN_LABEL = re.compile("^\\s*" + _NON_ASCII + "{2,4}\\s*[:：]\\s*(?P<val>.*)$")
_CN_DATE = re.compile(r"20\d\d\D{1,2}\d{1,2}\D{1,2}\d{1,2}")
_EN_HEADER_LABEL = re.compile(r"^\s*(?:To|Cc|Bcc|Subject|Attachments?):", re.I)
_ADDRESS_END = re.compile(r"<[^<>@\s]+@[^<>\s]+>\s*$")
_GMAIL_DATE = re.compile(
    r"^\s*[A-Z][a-z]{2},\s+[A-Z][a-z]{2}\s+\d{1,2},\s+20\d\d\s+at\s+\d{1,2}:\d{2}\s*[AP]M\s*$"
)
MIN_PREAMBLE_CHARS = 200


def _is_date_value(val: str) -> bool:
    if _CN_DATE.search(val):
        return True
    return bool(re.search(r"\b20\d\d\b", val) and re.search(r"\d{1,2}:\d{2}", val))


def _next_nonblank(lines: list[str], i: int) -> int | None:
    j = i + 1
    while j < len(lines) and not lines[j].strip():
        j += 1
    return j if j < len(lines) else None


def _header_at(lines: list[str], i: int):
    """If a message header starts at line i, return (sender, sent, index after header)."""
    line = lines[i]

    m = _ON_WROTE.match(line)
    if m:
        return m["who"], m["when"], i + 1

    m = _FROM.match(line)
    if m:
        for j in range(i + 1, min(i + 6, len(lines))):
            s = _SENT.match(lines[j])
            if s:
                return m["who"], s["when"], j + 1
        return None

    m = _CN_LABEL.match(line)
    if m:
        j = _next_nonblank(lines, i)
        if j is not None:
            d = _CN_LABEL.match(lines[j])
            if d and _is_date_value(d["val"]):
                return m["val"], d["val"], j + 1
        return None

    if _ADDRESS_END.search(line) and not _EN_HEADER_LABEL.match(line):
        j = _next_nonblank(lines, i)
        if j is not None and _GMAIL_DATE.match(lines[j]):
            return line.strip(), lines[j].strip(), j + 1
    return None


def _strip_header_lines(lines: list[str]) -> list[str]:
    """Drop the To/Cc/Subject lines (and wrapped address lines) that follow a header."""
    k = 0
    while k < min(len(lines), 12):
        s = lines[k].strip()
        if not s or s.startswith("<") or _EN_HEADER_LABEL.match(s) or _CN_LABEL.match(s):
            k += 1
        else:
            break
    return lines[k:]


def _clean(lines: list[str]) -> str:
    text = "\n".join(l.rstrip() for l in lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _clean_sender(who: str | None) -> str | None:
    """'ops@minchem.in ... On Behalf Of Eva Zhang' -> 'Eva Zhang'."""
    if not who:
        return who
    m = re.search(r"On Behalf Of\s+(.+)$", who, re.I)
    return m.group(1).strip() if m else who


def split_thread(full_text: str | None) -> list[dict]:
    """Return messages in printed order (newest first): [{sender, sent, text}]."""
    lines = [
        l for l in (full_text or "").splitlines()
            if not _PAGE_MARK.match(l.strip()) and not _PRINT_NOISE.match(l)
            and not _PRINT_TITLE.match(l)
    ]

    starts = []
    i = 0
    while i < len(lines):
        header = _header_at(lines, i)
        if header:
            starts.append((i, header))
            i = header[2]
        else:
            i += 1

    messages = []
    first = starts[0][0] if starts else len(lines)
    preamble = _clean(lines[:first])
    if len(preamble) >= MIN_PREAMBLE_CHARS or not starts:
        if preamble:
            messages.append({"sender": None, "sent": None, "text": preamble})

    for k, (_, (who, when, end)) in enumerate(starts):
        stop = starts[k + 1][0] if k + 1 < len(starts) else len(lines)
        messages.append({
            "sender": _clean_sender(who),
            "sent": when,
            "text": _clean(_strip_header_lines(lines[end:stop])),
        })
    return messages


def _safe(text) -> str:
    return str(text).encode("ascii", "replace").decode()


if __name__ == "__main__":
    from app.tender_store import get_thread_documents

    parser = argparse.ArgumentParser(description="Show how a stored email-thread document splits into messages.")
    parser.add_argument("--id", type=int, required=True, help="document id")
    args = parser.parse_args()

    doc = next((d for d in get_thread_documents(True) if d["id"] == args.id), None)
    if doc is None:
        raise SystemExit(f"No email-thread document with id {args.id}")

    messages = split_thread(doc["full_text"])
    print(f"{len(messages)} message(s)")
    for n, m in enumerate(messages, start=1):
        preview = " ".join(m["text"].split())[:70]
        print(f"#{n:<2} {len(m['text']):>5} chars | {_safe(m['sender'])[:34]:<34} | "
              f"{_safe(m['sent'])[:28]:<28} | {_safe(preview)}")