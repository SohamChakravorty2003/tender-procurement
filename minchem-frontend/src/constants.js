// Mirrors EmailCategory / EmailRole in app/models.py (the API does not expose them).
export const CATEGORIES = [
  { value: "quotation_request", label: "Quotation request" },
  { value: "follow_up", label: "Follow-up" },
  { value: "order_confirmation", label: "Order confirmation" },
  { value: "shipment_update", label: "Shipment update" },
  { value: "document_collection", label: "Document collection" },
  { value: "issue_resolution", label: "Issue resolution" },
  { value: "other", label: "Other" }
];

export const ROLES = [
  { value: "tender_issue", label: "Tender issue" },
  { value: "vendor_bid", label: "Vendor bid" },
  { value: "other", label: "Other" }
];

export const categoryLabel = (v) => CATEGORIES.find((c) => c.value === v)?.label || v || "Other";
export const roleLabel = (v) => ROLES.find((r) => r.value === v)?.label || v || "Other";

export const DOC_TYPE_LABELS = {
  email_thread: "Email threads",
  purchase_order: "Purchase orders",
  sales_contract: "Sales contracts",
  shipping_documents: "Shipping documents",
  quotation: "Quotations",
  tender_rfq: "Tender / RFQ",
  other: "Other",
  unclassified: "Not analysed yet"
};

export const docTypeLabel = (v) => DOC_TYPE_LABELS[v] || (v || "").replace(/_/g, " ");
export const prettyWord = (v) => (v || "").replace(/_/g, " ");

   export function formatOffer(offer) {
     if (!offer) return "—";
     if (offer.unit_price != null) return `${offer.currency || ""} ${offer.unit_price}`.trim();
     return offer.price_text || "—";
   }

// "Name <a@b.com>" -> "Name"; falls back to the address.
export function senderName(sender) {
  if (!sender) return "";
  const m = sender.match(/^\s*"?([^"<]*?)"?\s*<([^>]+)>\s*$/);
  return m ? m[1].trim() || m[2] : sender;
}
