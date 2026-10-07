import { categoryLabel, roleLabel } from "../constants";

export function Badge({ children, tone = "grey", title }) {
  return (
    <span className={`badge badge--${tone}`} title={title}>
      {children}
    </span>
  );
}

const CATEGORY_TONE = {
  quotation_request: "blue",
  follow_up: "grey",
  order_confirmation: "green",
  shipment_update: "teal",
  document_collection: "purple",
  issue_resolution: "red",
  other: "grey"
};

const ROLE_TONE = { tender_issue: "amber", vendor_bid: "green", other: "grey" };

export function CategoryBadge({ value }) {
  return <Badge tone={CATEGORY_TONE[value] || "grey"}>{categoryLabel(value)}</Badge>;
}

export function RoleBadge({ value }) {
  // "other" carries no information worth a badge on every row.
  if (!value || value === "other") return null;
  return <Badge tone={ROLE_TONE[value] || "grey"}>{roleLabel(value)}</Badge>;
}

export function TenderChip({ id, onOpen }) {
  if (!id) return null;
  return (
    <button
      type="button"
      className="chip"
      title="Open tender"
      onClick={(e) => {
        e.stopPropagation();
        onOpen(id);
      }}
    >
      {id}
    </button>
  );
}
