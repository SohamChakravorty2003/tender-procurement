"""
Classifier evaluation: runs classify_service.analyze_email over the test-email
JSON files and compares with the expected labels.

- Calls analyze_email directly: no email_analysis cache, no thread inheritance,
  nothing is written to procurement.db.
- Rows flagged "ambiguous" are reported separately from the firm ones.

    uv run python -m app.eval_classifier
    uv run python -m app.eval_classifier --files path\\a.json path\\b.json
    uv run python -m app.eval_classifier --ids rkcb_04 graphite_03
Default files: every *test_emails.json under MINCHEM-DATA-TRAINING.
"""

import argparse
import json
import time
from pathlib import Path

from app.classify_service import analyze_email

ROOT = Path(__file__).resolve().parent.parent
PAUSE_SECONDS = 2


def _safe(text) -> str:
    return str(text).encode("ascii", "replace").decode()


def _norm_id(value) -> str | None:
    return value.strip().upper() if isinstance(value, str) and value.strip() else None


def load_emails(files: list[Path]) -> list[dict]:
    emails = []
    for f in files:
        emails.extend(json.loads(f.read_text(encoding="utf-8")))
    return emails


def _pct(ok: int, total: int) -> str:
    return f"{ok}/{total} ({100 * ok / total:.0f}%)" if total else "0/0"


def run(emails: list[dict]) -> list[dict]:
    rows = []
    for e in emails:
        result = analyze_email(e["subject"], e["body"], e["sender"])
        row = {"email": e, "result": result}
        if result is None:
            row.update(cat_ok=False, role_ok=False, id_ok=False, got=("FAILED", "FAILED", None))
        else:
            got_id = _norm_id(result.tender_id)
            row.update(
                cat_ok=result.category.value == e["expected_category"],
                role_ok=result.email_role.value == e["expected_email_role"],
                id_ok=got_id == _norm_id(e.get("expected_tender_id")),
                got=(result.category.value, result.email_role.value, got_id),
            )
        rows.append(row)
        flag = "ok " if row["cat_ok"] and row["role_ok"] and row["id_ok"] else "DIFF"
        print(f"{flag} {e['id']:<12} {'(amb) ' if e['ambiguous'] else '      '}"
              f"{row['got'][0]:<20} {row['got'][1]:<13} id={row['got'][2]}")
        time.sleep(PAUSE_SECONDS)
    return rows


def report(rows: list[dict]) -> None:
    groups = {
        "firm": [r for r in rows if not r["email"]["ambiguous"]],
        "ambiguous": [r for r in rows if r["email"]["ambiguous"]],
        "all": rows,
    }
    print("\n=== Accuracy ===")
    print(f"{'':<10} {'category':<16} {'role':<16} {'tender_id':<16}")
    for name, g in groups.items():
        print(f"{name:<10} {_pct(sum(r['cat_ok'] for r in g), len(g)):<16} "
              f"{_pct(sum(r['role_ok'] for r in g), len(g)):<16} "
              f"{_pct(sum(r['id_ok'] for r in g), len(g)):<16}")

    failed = [r for r in rows if r["result"] is None]
    if failed:
        print(f"\n{len(failed)} call(s) failed: {', '.join(r['email']['id'] for r in failed)}")

    id_errors = [r for r in rows if not r["id_ok"] and r["result"] is not None]
    if id_errors:
        print("\n=== tender_id errors ===")
        for r in id_errors:
            print(f"{r['email']['id']}: expected {r['email'].get('expected_tender_id')}, got {r['got'][2]}")

    for title, amb in (("FIRM", False), ("AMBIGUOUS", True)):
        diffs = [r for r in rows if r["email"]["ambiguous"] == amb and not (r["cat_ok"] and r["role_ok"])]
        print(f"\n=== Disagreements: {title} rows ({len(diffs)}) ===")
        for r in diffs:
            e = r["email"]
            print(f"{e['id']}: category {e['expected_category']} -> {r['got'][0]}"
                  f" | role {e['expected_email_role']} -> {r['got'][1]}")
            print(f"    subject: {_safe(e['subject'])[:70]}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Evaluate the email classifier on the test emails.")
    parser.add_argument("--files", nargs="+", type=Path)
    parser.add_argument("--ids", nargs="+", help="only these email ids")
    args = parser.parse_args()

    files = args.files or sorted((ROOT / "MINCHEM-DATA-TRAINING").rglob("*test_emails.json"))
    if not files:
        raise SystemExit("No test_emails.json files found; pass --files")
    print("Files:", ", ".join(_safe(f) for f in files))

    emails = load_emails(files)
    if args.ids:
        emails = [e for e in emails if e["id"] in set(args.ids)]
    print(f"{len(emails)} emails\n")
    report(run(emails))