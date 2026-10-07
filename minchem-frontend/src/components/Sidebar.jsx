export default function Sidebar({ view, onNavigate, pendingReviews, status, lastUpdated, open, onClose }) {
  const items = [
    { key: "inbox", label: "Inbox" },
    { key: "tenders", label: "Tenders" },
    { key: "reviews", label: "Reviews", badge: pendingReviews }
  ];
  const active = view === "tender" ? "tenders" : view;

  return (
    <>
      {open && <div className="scrim" onClick={onClose} />}
      <aside className={`sidebar ${open ? "is-open" : ""}`}>
        <div className="brand">
          <span className="brand__mark">M</span>
          <div>
            <div className="brand__name">MinChem</div>
            <div className="brand__sub">Procurement desk</div>
          </div>
        </div>

        <nav className="nav">
          {items.map((it) => (
            <button
              key={it.key}
              className={`nav__item ${active === it.key ? "is-active" : ""}`}
              onClick={() => onNavigate(it.key)}
            >
              <span>{it.label}</span>
              {it.badge > 0 && <span className="nav__count">{it.badge}</span>}
            </button>
          ))}
        </nav>

        <button
          className={`btn btn--primary sidebar__compose ${view === "compose" ? "is-active" : ""}`}
          onClick={() => onNavigate("compose")}
        >
          New message
        </button>

        <div className="sidebar__footer">
          <span className={`dot dot--${status}`} />
          <div>
            <div>{status === "error" ? "Connection issue" : "Connected"}</div>
            <div className="muted small">
              {lastUpdated ? `Inbox updated ${lastUpdated.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}` : "Waiting for inbox"}
            </div>
          </div>
        </div>
      </aside>
    </>
  );
}
