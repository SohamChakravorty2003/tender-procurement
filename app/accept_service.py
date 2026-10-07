"""
Marks duplicate and accepted offers extracted from Gmail emails and email-thread
documents. No LLM calls.

  duplicate: same sender + same day + same unit price + same grade, currency,
             unit basis, and price terms/port as an earlier offer. Incomplete
             pricing context is a possible match, never an automatic duplicate.
  accepted:  an offer whose unit price appears in the tender's sales contract, on a
             price line or alone in a table cell. For each matching price the latest
             non-duplicate offer is marked (a contract with two grade prices marks
             two offers). If the marked offers come from different senders, nothing
             is marked and the tender is reported as ambiguous.

    uv run python -m app.accept_service                    # dry run, all tenders
    uv run python -m app.accept_service --tender TND-0002  # one tender
    uv run python -m app.accept_service --apply            # write the marks
Safe to re-run: the marks of a tender are replaced each time.
"""

import argparse
import json
import re
import time
from datetime import date

from app.tender_store import (
    get_contract_texts,
    get_offer_bids,
    get_tender_ids_with_offers,
    save_offer_marks,
)

MIN_PRICE = 10.0
_PRICE_LINE = re.compile(r"USD|US\$|\$|EUR|RMB|PRICE|/\s*MT|PER\s*(?:M/?T|TON)|PMT", re.I)
_ONLY_NUMBER = re.compile(r"^\s*\d[\d,]*(?:\.\d+)?\s*$")
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_ISO_CN = re.compile(r"(20\d\d)\s*[\u5e74/\-.]\s*(\d{1,2})\s*[\u6708/\-.]\s*(\d{1,2})")
_DMY = re.compile(r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?,?\s+(20\d\d)")
_MDY = re.compile(r"([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d\d)")
_ADDRESS = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def _safe(text) -> str:
    return str(text).encode("ascii", "replace").decode()


def parse_day(text: str | None) -> date | None:
    """Day of a date written in the formats seen in the threads; None if not recognised."""
    if not text:
        return None
    try:
        m = _ISO_CN.search(text)
        if m:
            return date(int(m[1]), int(m[2]), int(m[3]))
        m = _DMY.search(text)
        if m and m[2][:3].lower() in _MONTHS:
            return date(int(m[3]), _MONTHS[m[2][:3].lower()], int(m[1]))
        m = _MDY.search(text)
        if m and m[1][:3].lower() in _MONTHS:
            return date(int(m[3]), _MONTHS[m[1][:3].lower()], int(m[2]))
    except ValueError:
        return None
    return None


def sender_identities(sender: str | None) -> set[str]:
    """What identifies a sender: the email address and/or the name (letters and digits only)."""
    s = sender or ""
    ids = set()
    m = _ADDRESS.search(s)
    if m:
        ids.add("a:" + m[0].lower())
    name = re.sub(r"[^a-z0-9]", "", _ADDRESS.sub("", s).lower())
    if len(name) >= 3:
        ids.add("n:" + name)
    return ids or {"u:" + (re.sub(r"[^a-z0-9]", "", s.lower()) or "unknown")}


def _cluster_senders(offers: list[dict]) -> None:
    """Give every offer a sender_key; senders sharing an address or a name get the same key."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for o in offers:
        ids = sorted(o["identities"])
        for other in ids[1:]:
            parent[find(other)] = find(ids[0])
    for o in offers:
        o["sender_key"] = find(sorted(o["identities"])[0])


def contract_prices(tender_id: str) -> dict[float, str]:
    """Numbers printed in the tender's sales contracts on price-looking lines or alone
    on a line (table cells), with the line they came from."""
    prices: dict[float, str] = {}
    for doc in get_contract_texts(tender_id):
        for line in (doc["full_text"] or "").splitlines():
            line = re.sub(r"\s+([,.])\s*(?=\d)", r"\1", line)   # 'USD130 ,050 .00' -> 'USD130,050.00'
            if not (_PRICE_LINE.search(line) or _ONLY_NUMBER.match(line)):
                continue
            for token in re.findall(r"\d[\d,]*(?:\.\d+)?", line):
                try:
                    value = float(token.replace(",", ""))
                except ValueError:
                    continue
                if value >= MIN_PRICE:
                    prices.setdefault(round(value, 2), line.strip()[:90])
    return prices


def _order(o: dict) -> tuple:
    """Order offers by parsed message day, then stable source details."""
    source_id = str(o.get("document_id") or o.get("email_id") or "")
    return (
        o["day"] or date.min,
        (o.get("offered_at") or "").strip(),
        source_id,
        o["offer_seq"] or 0,
    )


def _spec_key(o: dict) -> str:
    data = o["data"]
    products = data.get("products_offered") or []
    if isinstance(products, str):
        products = [products]
    stated = " ".join(str(value) for value in [*products, data.get("specifications") or ""] if value)
    return re.sub(r"[^a-z0-9]", "", stated.casefold())


def _text_key(value: str | None) -> str:
    """Compare stated terms case-insensitively while preserving their wording."""
    return re.sub(r"\s+", " ", str(value or "").strip().casefold())


def _unit_basis_key(data: dict) -> str:
    """Return a recognised price unit; ambiguous/missing units remain unknown."""
    text = " ".join(str(data.get(field) or "") for field in ("price_text", "price_terms"))
    patterns = (
        (r"\b(?:per\s+)?(?:metric\s+ton|metric\s+tonne|mt|m/t)\b|/\s*mt\b|\bpmt\b", "metric ton"),
        (r"\b(?:per\s+)?kg\b|/\s*kg\b", "kg"),
        (r"\b(?:per\s+)?tonne\b|/\s*tonne\b", "tonne"),
        (r"\b(?:per\s+)?ton\b|/\s*ton\b", "ton"),
        (r"\b(?:per\s+)?lb\b|/\s*lb\b", "lb"),
    )
    for pattern, unit in patterns:
        if re.search(pattern, text, re.I):
            return unit
    return ""


def _terms_key(value: str | None) -> str:
    """Keep exact stated basis/port wording; known Incoterms need a location."""
    terms = _text_key(value)
    match = re.match(r"^(fob|cif|cfr|fas|fca|cpt|cip|dap|dpu|ddp|exw)\b[\s,;:-]*(.*)$", terms)
    if match and not match.group(2).strip():
        return ""
    return terms


def _duplicate_key(o: dict) -> tuple | None:
    """Build a strict key; incomplete quote context cannot auto-dedupe."""
    data = o["data"]
    spec = _spec_key(o)
    currency = _text_key(data.get("currency"))
    unit = _unit_basis_key(data)
    terms = _terms_key(data.get("price_terms"))
    if not all((spec, currency, unit, terms)):
        return None
    return (
        o["sender_key"], o["day"] or (o["offered_at"] or "").strip(),
        round(o["price"], 2), spec, currency, unit, terms,
        _text_key(data.get("quantity")),
    )


def _possible_match(a: dict, b: dict) -> bool:
    """Keep review candidates to compatible or incomplete price contexts."""
    if _duplicate_key(a) == _duplicate_key(b) and _duplicate_key(a) is not None:
        return False
    da, db = a["data"], b["data"]
    currency_a, currency_b = _text_key(da.get("currency")), _text_key(db.get("currency"))
    unit_a, unit_b = _unit_basis_key(da), _unit_basis_key(db)
    terms_a, terms_b = _terms_key(da.get("price_terms")), _terms_key(db.get("price_terms"))
    # Known conflicts in currency, unit basis, or incoterm/port are not duplicate
    # candidates. Missing context and wording differences remain reviewable.
    if currency_a and currency_b and currency_a != currency_b:
        return False
    if unit_a and unit_b and unit_a != unit_b:
        return False
    if terms_a and terms_b and terms_a != terms_b:
        return False
    return True


def _llm_review_candidate(first: dict, second: dict) -> dict:
    """Ask the document LLM for a review hint; never use its answer to mark duplicates."""
    from langchain_core.output_parsers import StrOutputParser
    from langchain_core.prompts import ChatPromptTemplate
    from app.classify_service import _parse_llm_json
    from app.document_service import doc_llm

    prompt = ChatPromptTemplate.from_template(
        """Compare two extracted supplier offers that share sender, day, and numeric unit price.
Decide only whether they may describe the same offer. Missing or conflicting currency,
unit basis, grade, quantity, or delivery terms are uncertainty, not proof of equivalence.
Do not convert currencies or infer unstated facts. Return JSON only:
{{\"likely_same\": true|false, \"reason\": \"short explanation\", \"evidence\": [\"quoted field/value\"]}}

Offer A: {offer_a}
Offer B: {offer_b}"""
    )
    fields = ("vendor_company", "unit_price", "currency", "price_terms", "price_text",
              "products_offered", "quantity", "specifications")
    compact = lambda offer: {key: offer["data"].get(key) for key in fields}
    try:
        raw = (prompt | doc_llm | StrOutputParser()).invoke({
            "offer_a": json.dumps(compact(first), ensure_ascii=False),
            "offer_b": json.dumps(compact(second), ensure_ascii=False),
        })
        time.sleep(2)
        result = _parse_llm_json(raw)
        if isinstance(result, dict) and isinstance(result.get("likely_same"), bool):
            return result
    except Exception as exc:
        return {"likely_same": None, "reason": f"LLM review unavailable: {exc}", "evidence": []}
    return {"likely_same": None, "reason": "LLM returned an invalid review result", "evidence": []}


def plan_tender(tender_id: str, llm_review: bool = False) -> dict:
    offers = []
    for r in get_offer_bids(tender_id):
        data = json.loads(r["extracted_json"] or "{}")
        offers.append({
            **r,
            "data": data,
            # Gmail's From address is authoritative for email bids. Include the
            # extracted sender/name too so it can match a PDF-thread offer.
            "identities": sender_identities(
                " ".join(filter(None, [r.get("vendor_email"), data.get("sender"), r.get("vendor_name")]))
            ),
            "day": parse_day(r["offered_at"]),
            "price": data.get("unit_price"),
        })
    _cluster_senders(offers)

    seen: dict[tuple, int] = {}
    duplicates: list[tuple[int, int]] = []
    candidates: list[tuple[dict, dict]] = []
    for o in sorted(offers, key=_order):
        if o["price"] is None:
            continue
        key = _duplicate_key(o)
        if key is not None and key in seen:
            duplicates.append((o["id"], seen[key]))
        elif key is not None:
            seen[key] = o["id"]
    ordered_offers = sorted(offers, key=_order)
    for index, first in enumerate(ordered_offers):
        if first["price"] is None:
            continue
        for second in ordered_offers[index + 1:]:
            same_day = first["day"] == second["day"] if first["day"] and second["day"] else (
                bool(first["offered_at"]) and first["offered_at"] == second["offered_at"]
            )
            if (first["sender_key"] == second["sender_key"] and same_day
                    and round(first["price"], 2) == round(second["price"], 2)
                    and _possible_match(first, second)):
                candidates.append((first, second))
    if llm_review:
        candidates = [(a, b, _llm_review_candidate(a, b)) for a, b in candidates]
    dup_ids = {d for d, _ in duplicates}

    prices = contract_prices(tender_id)
    chosen: dict[float, dict] = {}
    for o in offers:
        if o["id"] in dup_ids or o["price"] is None:
            continue
        p = round(o["price"], 2)
        if p in prices and (p not in chosen or _order(o) > _order(chosen[p])):
            chosen[p] = o

    if not prices:
        status = "no contract price found"
    elif not chosen:
        status = "no offer matches the contract"
    elif len({o["sender_key"] for o in chosen.values()}) > 1:
        status = "ambiguous (matches come from different senders)"
    else:
        status = "ok"

    return {
        "tender_id": tender_id, "offers": offers, "duplicates": duplicates,
        "possible_duplicates": candidates,
        "prices": prices, "chosen": chosen, "status": status,
        "accepted_ids": [o["id"] for o in chosen.values()] if status == "ok" else [],
    }


def show(plan: dict) -> None:
    print(f"\n{plan['tender_id']}: {len(plan['offers'])} offers, "
          f"{len(plan['duplicates'])} duplicate(s), contract prices found: {len(plan['prices'])}")
    by_id = {o["id"]: o for o in plan["offers"]}
    for dup, orig in plan["duplicates"]:
        d, o = by_id[dup], by_id[orig]
        d_source = f"email {d['email_id']}" if d.get("email_id") else f"doc {d['document_id']}"
        o_source = f"email {o['email_id']}" if o.get("email_id") else f"doc {o['document_id']}"
        grade = d["data"].get("specifications") or d["data"].get("products_offered")
        print(f"   duplicate: bid {dup} ({d_source}) of bid {orig} ({o_source})"
              f" | {_safe(d['sender_key'])} | {_safe(d['offered_at'])} | {d['price']}"
              f" | {_safe(str(grade))[:30]}")
    for candidate in plan["possible_duplicates"]:
        a, b = candidate[:2]
        relation = candidate[2] if len(candidate) > 2 else None
        message = "needs review"
        if relation:
            message = f"LLM suggests possible match: {relation['likely_same']} ({relation['reason']})"
        print(f"   possible match: bids {a['id']} and {b['id']} | {message}; not auto-marked")
    for price, o in sorted(plan["chosen"].items()):
        source = f"email {o['email_id']}" if o.get("email_id") else f"doc {o['document_id']}"
        print(f"   match {price}: bid {o['id']} ({source} #{o['offer_seq']}) "
              f"{o['data'].get('kind')} | {_safe(o['sender_key'])} | {_safe(o['offered_at'])}")
        print(f"      contract line: {_safe(plan['prices'][price])}")
    print(f"   result: {plan['status']}; accepted bid ids: {plan['accepted_ids']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Mark duplicate and accepted offers.")
    parser.add_argument("--tender", help="only this tender")
    parser.add_argument("--apply", action="store_true", help="write the marks")
    parser.add_argument("--llm-review", action="store_true",
                        help="ask the LLM to suggest whether possible matches may be the same; never auto-marks them")
    args = parser.parse_args()

    tenders = [args.tender] if args.tender else get_tender_ids_with_offers()
    for tid in tenders:
        plan = plan_tender(tid, llm_review=args.llm_review)
        show(plan)
        if args.apply:
            save_offer_marks(tid, plan["duplicates"], plan["accepted_ids"])
    print("\n" + ("Marks written." if args.apply else "Dry run only. Re-run with --apply to write."))
