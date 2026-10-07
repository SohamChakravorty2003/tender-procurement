import { useCallback, useEffect, useRef, useState } from "react";
import { getEmails, getReviews } from "./api";
import Sidebar from "./components/Sidebar";
import InboxView from "./components/InboxView";
import ComposeView from "./components/ComposeView";
import TendersView from "./components/TendersView";
import TenderDetail from "./components/TenderDetail";
import ReviewsView from "./components/ReviewsView";

const AUTO_REFRESH_MS = 30000;

export default function App() {
  const [view, setView] = useState("inbox"); // inbox | compose | tenders | tender | reviews
  const [tenderId, setTenderId] = useState(null);
  const [menuOpen, setMenuOpen] = useState(false);

  const [emails, setEmails] = useState([]);
  const [loaded, setLoaded] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState(null);
  const [lastUpdated, setLastUpdated] = useState(null);
  const fetching = useRef(false);

  const [pendingReviews, setPendingReviews] = useState(0);

  const loadEmails = useCallback(async () => {
    if (fetching.current) return; // /emails can be slow (it classifies new mail): never overlap calls
    fetching.current = true;
    setRefreshing(true);
    try {
      setEmails(await getEmails());
      setLastUpdated(new Date());
      setError(null);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoaded(true);
      setRefreshing(false);
      fetching.current = false;
    }
  }, []);

  const loadReviewCount = useCallback(() => {
    getReviews("pending").then((r) => setPendingReviews(r.length)).catch(() => {});
  }, []);

  useEffect(() => {
    loadEmails();
    loadReviewCount();
    const id = setInterval(loadEmails, AUTO_REFRESH_MS);
    return () => clearInterval(id);
  }, [loadEmails, loadReviewCount]);

  function navigate(next) {
    setView(next);
    setMenuOpen(false);
    if (next === "reviews") loadReviewCount();
  }

  function openTender(id) {
    setTenderId(id);
    setView("tender");
    setMenuOpen(false);
  }

  return (
    <div className="shell">
      <Sidebar
        view={view}
        onNavigate={navigate}
        pendingReviews={pendingReviews}
        status={error ? "error" : "ok"}
        lastUpdated={lastUpdated}
        open={menuOpen}
        onClose={() => setMenuOpen(false)}
      />
      <div className="main">
        <div className="topbar">
          <button className="btn btn--small" onClick={() => setMenuOpen(true)} aria-label="Open menu">Menu</button>
          <strong>MinChem</strong>
        </div>
        <main className="content">
          {view === "inbox" && (
            <InboxView
              emails={emails}
              loaded={loaded}
              refreshing={refreshing}
              error={error}
              onRefresh={loadEmails}
              onOpenTender={openTender}
            />
          )}
          {view === "compose" && <ComposeView />}
          {view === "tenders" && <TendersView onOpenTender={openTender} />}
          {view === "tender" && <TenderDetail tenderId={tenderId} onBack={() => setView("tenders")} />}
          {view === "reviews" && <ReviewsView onChanged={loadReviewCount} onOpenTender={openTender} />}
        </main>
      </div>
    </div>
  );
}
