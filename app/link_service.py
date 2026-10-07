"""
Tender linking: groups documents into tenders using the reference numbers found in
them (increment 3). Two documents that share a reference number belong to the same
tender, and links are followed transitively (A shares a number with B, B with C).

    uv run python -m app.link_service                  # dry run: show the proposed tenders
    uv run python -m app.link_service --apply          # write tenders and links to the database
    uv run python -m app.link_service --reset-generated  # drop generated tenders (TND-...) so they are re-derived
    uv run python -m app.link_service --check "MINCHEM-DATA-TRAINING" [--level 2]
        # compare the stored result with the folder structure (the folder at
        # depth LEVEL below the root is taken as the tender)

Rules:
- Reference numbers are compared with punctuation and spaces removed, whatever their
  type (a PO number printed as a contract number still matches).
- A tender is never merged automatically: if one group of documents already belongs to
  two different tenders, a 'conflict' review is queued and nothing is changed.
- Documents sharing no reference number stay unlinked.
- Safe to re-run: documents already in a tender keep it.
"""

import argparse
import re
from collections import defaultdict
from pathlib import Path

from app.tender_store import (
    add_link_review,
    assign_documents_to_tender,
    delete_generated_tenders,
    get_document_references,
    get_documents_for_linking,
    next_generated_tender_id,
    normalize_tender_id,
)

MIN_KEY_LEN = 5
MAX_DOCS_PER_KEY = 12   # a number printed in more documents than this is not an identifier
# Values containing a place name are addresses or ports, not identifiers.
_PLACE_WORDS = (
    "SINGAPORE", "KOLKATA", "INDIA", "MUNDRA", "VIZAG",
    "CHENNAI", "ENNORE", "TIANJIN", "XINGANG", "NHAVASHEVA",
)


def _key(value: str) -> str | None:
    key = re.sub(r"[^A-Z0-9]", "", value.upper())
    if len(key) < MIN_KEY_LEN or not any(c.isdigit() for c in key):
        return None
    if any(word in key for word in _PLACE_WORDS):
        return None
    return key


def _safe(text) -> str:
    return str(text).encode("ascii", "replace").decode()


def plan_links() -> list[dict]:
    """Work out, without writing anything, which documents belong together.
    Returns one entry per group of documents with: action (create / attach /
    unchanged / conflict / unlinked), tenders (tenders the group already names),
    keys (shared reference numbers), members (document rows), unassigned (ids)."""
    docs = {d["id"]: d for d in get_documents_for_linking()}
    key_docs: dict[str, set[int]] = defaultdict(set)
    claims: dict[int, set[str]] = defaultdict(set)   # document id -> tenders it already names

    for ref in get_document_references():
        doc_id = int(ref["source_id"])
        if doc_id not in docs:
            continue
        if ref["ref_type"] == "tender_id":
            tid = normalize_tender_id(ref["ref_value"])
            if tid:
                claims[doc_id].add(tid)
            continue
        key = _key(ref["ref_value"])
        if key:
            key_docs[key].add(doc_id)

    for doc in docs.values():
        if doc["tender_id"]:
            claims[doc["id"]].add(doc["tender_id"])
    for doc_id, tender_ids in list(claims.items()):
        for tid in tender_ids:
            key_docs["T:" + tid].add(doc_id)   # documents naming the same tender belong together

    parent = {i: i for i in docs}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    shared: dict[str, list[int]] = {}
    for key, ids in key_docs.items():
        ids = sorted(ids)
        if len(ids) < 2:
            continue
        if not key.startswith("T:") and len(ids) > MAX_DOCS_PER_KEY:
            continue
        shared[key] = ids
        for other in ids[1:]:
            parent[find(other)] = find(ids[0])

    members_by_root: dict[int, list[int]] = defaultdict(list)
    for doc_id in sorted(docs):
        members_by_root[find(doc_id)].append(doc_id)
    keys_by_root: dict[int, list[str]] = defaultdict(list)
    for key, ids in shared.items():
        if not key.startswith("T:"):
            keys_by_root[find(ids[0])].append(key)

    plan = []
    for root, members in sorted(members_by_root.items(), key=lambda kv: kv[1][0]):
        tenders = sorted({t for m in members for t in claims.get(m, ())})
        unassigned = [m for m in members if not docs[m]["tender_id"]]
        if len(tenders) >= 2:
            action = "conflict"
        elif len(tenders) == 1:
            action = "attach" if unassigned else "unchanged"
        elif len(members) >= 2:
            action = "create"
        else:
            action = "unlinked"
        plan.append({
            "action": action,
            "tenders": tenders,
            "keys": sorted(keys_by_root.get(root, [])),
            "members": [docs[m] for m in members],
            "unassigned": unassigned,
            "claims": {m: sorted(claims[m]) for m in members if claims.get(m)},
            "new_tender": None,
        })
    return plan


def apply_plan(plan: list[dict]) -> dict:
    summary = {"tenders_created": 0, "documents_linked": 0, "reviews_queued": 0}

    for entry in plan:
        action = entry["action"]

        if action == "create":
            tender_id = next_generated_tender_id()
            assign_documents_to_tender(
                tender_id, [m["id"] for m in entry["members"]], "reference", id_origin="generated"
            )
            entry["new_tender"] = tender_id
            summary["tenders_created"] += 1
            summary["documents_linked"] += len(entry["members"])

        elif action == "attach":
            assign_documents_to_tender(entry["tenders"][0], entry["unassigned"], "reference")
            summary["documents_linked"] += len(entry["unassigned"])

        elif action == "conflict":
            tenders = entry["tenders"]
            for i, a in enumerate(tenders):
                for b in tenders[i + 1:]:
                    # the document that bridges the two tenders is the one not yet assigned
                    source = entry["unassigned"][0] if entry["unassigned"] else min(
                        d for d, named in entry["claims"].items() if b in named
                    )
                    queued = add_link_review(
                        review_type="conflict",
                        source_kind="document",
                        source_id=str(source),
                        tender_id=a,
                        other_tender_id=b,
                        reason="Documents sharing reference numbers belong to different tenders"
                               f" (shared: {', '.join(entry['keys'][:5])})",
                    )
                    summary["reviews_queued"] += int(queued)

    return summary


def show_plan(plan: list[dict]) -> None:
    for entry in plan:
        action = entry["action"]
        if action == "unlinked":
            continue
        if action == "create":
            title = f"NEW TENDER {entry['new_tender'] or '(id assigned on --apply)'}"
        elif action == "attach":
            title = f"ADD {len(entry['unassigned'])} document(s) to {entry['tenders'][0]}"
        elif action == "unchanged":
            title = f"UNCHANGED {entry['tenders'][0]}"
        else:
            title = f"CONFLICT between {' and '.join(entry['tenders'])}: needs review, nothing changed"
        print(f"\n{title}  ({len(entry['members'])} documents)")
        if entry["keys"]:
            print(f"   linked by: {_safe(', '.join(entry['keys'][:6]))}")
        for d in entry["members"]:
            print(f"   [{d['id']}] {d['document_type'] or '?'} | {_safe(d['file_name'])[:70]}")

    unlinked = [e["members"][0] for e in plan if e["action"] == "unlinked"]
    if unlinked:
        print(f"\nUNLINKED ({len(unlinked)} documents share no reference number with any other):")
        for d in unlinked:
            print(f"   [{d['id']}] {d['document_type'] or '?'} | {_safe(d['file_name'])[:70]}")


def check_against_folders(root: str, level: int) -> None:
    """Compare the stored tenders with the folder structure of the sample data."""
    root_path = Path(root)
    expected: dict[str, set[str]] = defaultdict(set)
    for pdf in root_path.rglob("*.pdf"):
        parts = pdf.relative_to(root_path).parts
        expected[pdf.name].add(parts[level - 1] if len(parts) > level else "(top level)")

    by_folder: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    folders_of_tender: dict[str, set[str]] = defaultdict(set)
    not_found = []

    for doc in get_documents_for_linking():
        folders = expected.get(doc["file_name"])
        if not folders:
            not_found.append(doc["file_name"])
            continue
        folder = " | ".join(sorted(folders))
        tender = doc["tender_id"] or "(unlinked)"
        by_folder[folder][tender] += 1
        if doc["tender_id"]:
            folders_of_tender[doc["tender_id"]].add(folder)

    print("Folder -> tenders found in the database:")
    for folder in sorted(by_folder):
        counts = by_folder[folder]
        ok = len(counts) == 1 and "(unlinked)" not in counts
        detail = ", ".join(f"{t} x{n}" for t, n in sorted(counts.items()))
        print(f"  {'OK   ' if ok else 'CHECK'} {_safe(folder)}: {detail}")

    merged = {t: f for t, f in folders_of_tender.items() if len(f) > 1}
    if merged:
        print("\nTenders that span several folders (possible wrong merge):")
        for t, folders in sorted(merged.items()):
            print(f"  {t}: {_safe(' ; '.join(sorted(folders)))}")
    if not_found:
        print(f"\n{len(not_found)} stored document(s) not found under {root}: {_safe(', '.join(not_found[:5]))}")


def assign_manual(doc_id: int, tender_id: str) -> None:
    """Human decision: put one unlinked document into a tender (creating it if needed)."""
    doc = next((d for d in get_documents_for_linking() if d["id"] == doc_id), None)
    if not doc:
        raise SystemExit(f"No document with id {doc_id}")
    if doc["tender_id"]:
        raise SystemExit(f"Document {doc_id} is already in {doc['tender_id']}; not changed")
    assign_documents_to_tender(tender_id, [doc_id], "manual")
    print(f"Document {doc_id} added to {normalize_tender_id(tender_id)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Group documents into tenders by shared reference numbers.")
    parser.add_argument("--apply", action="store_true", help="write the result to the database")
    parser.add_argument("--check", metavar="ROOT", help="compare the stored tenders with this folder tree")
    parser.add_argument("--level", type=int, default=2, help="folder depth (below ROOT) that names the tender")
    parser.add_argument("--reset-generated", action="store_true",
                        help="remove generated tenders and unlink their documents (they are re-derived by the next run)")
    parser.add_argument("--assign", nargs=2, metavar=("DOC_ID", "TENDER_ID"),
                        help="manually put one unlinked document into a tender")
    args = parser.parse_args()

    if args.assign:
        assign_manual(int(args.assign[0]), args.assign[1])
    elif args.reset_generated:
        print(f"Removed {delete_generated_tenders()} generated tender(s). Run again (dry run, then --apply) to rebuild.")
    elif args.check:
        check_against_folders(args.check, args.level)
    else:
        plan = plan_links()
        summary = apply_plan(plan) if args.apply else None
        show_plan(plan)
        print("\n" + (f"Applied: {summary}" if summary else "Dry run only. Re-run with --apply to write this."))