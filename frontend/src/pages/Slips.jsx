import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import Disclosure from "../components/Disclosure";

export default function Slips() {
  const [data, setData] = useState(null);

  useEffect(() => {
    api.slips().then(setData).catch(() => setData({ results: [], vip: false }));
  }, []);

  if (!data) return <div className="card"><div className="skeleton" style={{ height: 120 }} /></div>;

  if (!data.vip) {
    return (
      <>
        <h2 className="section-title">VIP slips</h2>
        <div className="card">
          <p style={{ marginTop: 0 }}>
            Curated accumulators — the day's strongest reads combined into one slip,
            with one leg per match so the combined price means something.
          </p>
          <Link to="/account" className="btn primary" style={{ textDecoration: "none", textAlign: "center" }}>
            See plans
          </Link>
        </div>
        <Disclosure />
      </>
    );
  }

  if (data.results.length === 0) {
    return (
      <div className="empty">
        <p>No slip today.</p>
        <p className="muted">
          A thin slate produces no slip — we don't pad one out to fill the day.
        </p>
      </div>
    );
  }

  return (
    <>
      <h2 className="section-title">Today's slips</h2>
      {data.results.map((slip) => (
        <article className="card" key={slip.id}>
          <div className="pick-head">
            <div className="teams">{slip.title}</div>
            {slip.total_odds && (
              <div className="confidence">
                <b>{slip.total_odds}</b>
                <small>combined</small>
              </div>
            )}
          </div>
          <div style={{ marginTop: 10 }}>
            {slip.predictions.map((leg) => (
              <div className="market-row" key={leg.id} style={{ padding: "7px 0" }}>
                <span className="label">
                  {leg.fixture.home} v {leg.fixture.away}
                </span>
                <span className="selection">
                  {leg.selection_label}
                  {leg.market_odds && <span className="odds"> @ {leg.market_odds}</span>}
                </span>
              </div>
            ))}
          </div>
        </article>
      ))}
      <Disclosure />
    </>
  );
}
