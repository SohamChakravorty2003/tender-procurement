import { useMemo, useState } from "react";

const COLORS = ["#2563eb", "#d97706", "#7c3aed", "#0d9488", "#db2777", "#65a30d", "#0891b2", "#9333ea"];
const text = (value) => typeof value === "string" ? value.trim() : "";
const valueText = (value) => value == null ? "" : String(value).trim();
const vendorKey = (bid) => text(bid.vendor_email || bid.vendor_name).toLowerCase() || "unknown";
const vendorName = (bid) => bid.vendor_name || bid.vendor_email || "Unknown vendor";
const hasPrice = (bid) => bid.offer?.unit_price != null && bid.offer.unit_price !== "" && Number.isFinite(Number(bid.offer.unit_price));
const priceLabel = (offer) => `${text(offer.currency) ? `${offer.currency} ` : ""}${offer.unit_price}`;
const specification = (offer) => text(offer.specifications) || (Array.isArray(offer.products_offered)
  ? offer.products_offered.filter(Boolean).join(", ") : text(offer.products_offered));

function sourceKey(bid, index) {
  if (bid.email_id) return `email:${bid.email_id}`;
  if (bid.document_id != null && bid.message_seq != null) return `document:${bid.document_id}:message:${bid.message_seq}`;
  // Legacy rows lack message_seq. Preserve the original date string; never parse it.
  if (bid.document_id != null && bid.offered_at) return `document:${bid.document_id}:date:${bid.offered_at}`;
  return `offer:${bid.id ?? index}`;
}

function groupReplies(offers) {
  const vendors = new Map();
  offers.forEach((bid, index) => {
    if (!hasPrice(bid) || bid.duplicate_of) return;
    const key = vendorKey(bid);
    if (!vendors.has(key)) vendors.set(key, { key, name: vendorName(bid), events: new Map() });
    const group = vendors.get(key);
    const eventId = sourceKey(bid, index);
    if (!group.events.has(eventId)) group.events.set(eventId, { key: eventId, documentId: bid.document_id, bids: [] });
    group.events.get(eventId).bids.push(bid);
  });
  return [...vendors.values()].map((group, index) => ({
    ...group,
    color: COLORS[index % COLORS.length],
    events: [...group.events.values()].map((event, replyIndex) => ({ ...event, reply: replyIndex + 1 })),
  }));
}

function basisKey(bid) {
  const offer = bid.offer || {};
  const products = Array.isArray(offer.products_offered) ? offer.products_offered.filter(Boolean).join(", ") : text(offer.products_offered);
  const parts = [specification(offer), products, text(offer.currency), text(offer.price_terms), valueText(offer.quantity)];
  return parts.every(Boolean) ? JSON.stringify(parts) : null;
}

function repeatedTracks(groups) {
  return groups.flatMap((group) => {
    const tracks = new Map();
    group.events.forEach((event) => event.bids.forEach((bid) => {
      const basis = basisKey(bid);
      if (!basis || event.documentId == null) return;
      const key = JSON.stringify([event.documentId, basis]);
      if (!tracks.has(key)) tracks.set(key, []);
      tracks.get(key).push({ bid, reply: event.reply });
    }));
    return [...tracks.values()]
      .filter((points) => new Set(points.map((point) => point.reply)).size > 1
        && new Set(points.map((point) => point.reply)).size === points.length)
      .map((points) => ({ group, points, key: `${group.key}:${points[0].bid.id}` }));
  });
}

function OfferInspector({ selected }) {
  if (!selected) return <aside className="offer-inspector"><h4>Offer details</h4><p className="muted">Select a price tier to see its recorded details.</p></aside>;
  const { bid, reply } = selected;
  const offer = bid.offer || {};
  const fields = [
    ["Vendor", vendorName(bid)], ["Vendor email", bid.vendor_email], ["Supplier reply", `Reply ${reply}`],
    ["Unit price", priceLabel(offer)], ["Price terms (original wording)", offer.price_terms],
    ["Specifications", offer.specifications],
    ["Products", Array.isArray(offer.products_offered) ? offer.products_offered.join(", ") : offer.products_offered],
    ["Quantity", offer.quantity], ["Delivery days", offer.delivery_days], ["Delivery details", offer.delivery_text],
    ["Payment terms", offer.payment_terms], ["Warranty/support", offer.warranty_support], ["Validity", offer.validity],
    ["Exceptions/deviations", offer.exceptions_or_deviations], ["Price wording", offer.price_text],
    ["Offered at (as recorded)", bid.offered_at], ["Status", bid.accepted ? "Accepted offer" : "Not marked accepted"],
  ].filter(([, value]) => value !== null && value !== undefined && value !== "");
  return <aside className="offer-inspector">
    <div className="offer-inspector__heading"><h4>Offer details</h4>{bid.accepted && <span className="badge badge--green">Accepted offer</span>}</div>
    <dl className="offer-inspector__fields">{fields.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{String(value)}</dd></div>)}</dl>
  </aside>;
}

function MovementChart({ tracks, onSelect }) {
  if (!tracks.length) return <p className="no-history muted small">No repeated quote has a fully matching stated product, specification, currency, price terms, and quantity within one source document. Price movement is therefore not calculated.</p>;
  return <div className="movement-list">{tracks.map(({ group, points, key }) => {
    const first = points[0].bid.offer;
    const prices = points.map(({ bid }) => Number(bid.offer.unit_price));
    let min = Math.min(...prices), max = Math.max(...prices);
    if (min === max) { min -= 1; max += 1; }
    const firstReply = points[0].reply, lastReply = points[points.length - 1].reply;
    const x = (reply) => 30 + (reply - firstReply) * 300 / (lastReply - firstReply);
    const y = (price) => 75 - (price - min) * 50 / (max - min);
    return <div className="movement-track" key={key}>
      <strong>{group.name}</strong>
      <span className="muted small">{specification(first)} · {first.currency} · {first.price_terms} · quantity {first.quantity}</span>
      <svg viewBox="0 0 360 115" role="img" aria-label={`Price movement for ${group.name}: ${specification(first)}`}>
        <polyline fill="none" stroke={group.color} strokeWidth="3" points={points.map(({ bid, reply }) => `${x(reply)},${y(Number(bid.offer.unit_price))}`).join(" ")}/>
        {points.map(({ bid, reply }) => <g key={bid.id} role="button" tabIndex="0" aria-label={`Reply ${reply}, ${priceLabel(bid.offer)}`} onClick={() => onSelect(bid.id)} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onSelect(bid.id); } }}>
          {bid.accepted && <circle cx={x(reply)} cy={y(Number(bid.offer.unit_price))} r="10" fill="none" stroke="var(--ok)" strokeWidth="2.5"/>}
          <circle cx={x(reply)} cy={y(Number(bid.offer.unit_price))} r="5.5" fill={group.color}/>
          <text x={x(reply)} y="105" textAnchor="middle" className="chart__tick">Reply {reply}</text>
          <title>{`${group.name} · Reply ${reply} · ${priceLabel(bid.offer)}${bid.accepted ? " · Accepted" : ""}`}</title>
        </g>)}
      </svg>
      <span className="small">{priceLabel(points[0].bid.offer)} → {priceLabel(points[points.length - 1].bid.offer)}</span>
    </div>;
  })}</div>;
}

export default function PriceChart({ offers }) {
  const groups = useMemo(() => groupReplies(offers), [offers]);
  const [selectedId, setSelectedId] = useState(null);
  const selected = groups.flatMap((group) => group.events.flatMap((event) => event.bids.map((bid) => ({ bid, reply: event.reply })))).find(({ bid }) => bid.id === selectedId);
  const tracks = repeatedTracks(groups);
  const maxReplies = Math.max(1, ...groups.map((group) => group.events.length));
  const replyCount = groups.reduce((sum, group) => sum + group.events.length, 0);
  const priced = groups.flatMap((group) => group.events.flatMap((event) => event.bids));
  const bases = new Set(priced.map((bid) => JSON.stringify([
    text(bid.offer.currency), text(bid.offer.price_terms), specification(bid.offer), valueText(bid.offer.quantity),
  ])));
  const incomplete = priced.some((bid) => !text(bid.offer.currency) || !text(bid.offer.price_terms) || !specification(bid.offer) || !valueText(bid.offer.quantity));
  if (!priced.length) return <p className="empty">No valid non-duplicate priced offers are available for this chart.</p>;

  return <div className="offer-explorer">
    <p className="muted small quote-map__intro">{groups.length} supplier {groups.length === 1 ? "identity" : "identities"} · {replyCount} source {replyCount === 1 ? "reply" : "replies"} · {priced.length} price {priced.length === 1 ? "tier" : "tiers"}. Each card is one supplier message; its prices are separate quoted tiers.</p>
    {(bases.size > 1 || incomplete) && <p className="notice notice--error price-warning" role="status">Quotes have different or incomplete specifications or pricing bases. Prices are shown as stated, without a cheapest-to-most-expensive ranking or currency/term conversion.</p>}
    <div className="explorer-layout">
      <div className="quote-map" aria-label="Supplier replies and their quoted price tiers" style={{ "--reply-count": maxReplies }}>
        <div className="quote-map__scroll">
          <div className="quote-map__header"><span>Supplier</span>{Array.from({ length: maxReplies }, (_, index) => <span key={index}>Reply {index + 1}</span>)}</div>
          {groups.map((group) => <div key={group.key} className="quote-map__row">
            <div className="quote-map__vendor"><span className="quote-map__dot" style={{ background: group.color }}/><strong>{group.name}</strong><small>{group.events.length === 1 ? "1 source reply" : `${group.events.length} source replies`}</small></div>
            {Array.from({ length: maxReplies }, (_, index) => {
              const event = group.events[index];
              if (!event) return <div className="quote-map__empty" key={index}>No further reply in the saved data</div>;
              return <div className="quote-map__reply" key={event.key} style={{ borderTopColor: group.color }}>
                <div className="quote-map__reply-head"><strong className="quote-map__reply-label">Reply {event.reply}</strong><span>{event.bids.length} {event.bids.length === 1 ? "price tier" : "price tiers"}</span></div>
                {event.bids[0].offered_at && <p className="quote-map__date" title="Original date text; reply columns are source order, not a time axis">{event.bids[0].offered_at}</p>}
                <div className="quote-map__tiers">{event.bids.map((bid) => <button type="button" key={bid.id} className={`quote-map__tier${bid.accepted ? " is-accepted" : ""}${selectedId === bid.id ? " is-selected" : ""}`} onClick={() => setSelectedId(bid.id)} aria-pressed={selectedId === bid.id}>
                  <span className="quote-map__tier-top"><strong>{priceLabel(bid.offer)}</strong>{bid.accepted && <span className="badge badge--green">Accepted</span>}</span>
                  <span className="quote-map__spec">{specification(bid.offer) || "Specification not stated"}</span>
                  <span className="quote-map__terms">{bid.offer.price_terms || "Price terms not stated"}</span>
                </button>)}</div>
              </div>;
            })}
          </div>)}
        </div>
        <p className="muted small">Reply numbers follow the saved source order for each supplier; they are not dates. Separate PDFs do not establish a shared timeline. A card with several prices is one reply with several price tiers, not several negotiation rounds.</p>
      </div>
      <OfferInspector selected={selected}/>
    </div>
    <section className="verified-movement"><h4>Comparable price movement</h4><MovementChart tracks={tracks} onSelect={setSelectedId}/><p className="muted small">A line appears only when the same supplier repeats a stated product, specification, currency, exact price terms, and quantity in separate replies of one source document. Accepted means marked from the contract; it is not a price ranking.</p></section>
  </div>;
}
