import { useEffect, useState } from "react";
import { api } from "../api";

const MARKET_LABELS = {
  "1x2": "Match Result",
  dc: "Double Chance",
  ou_2_5: "Over/Under 2.5",
  btts: "Both Teams To Score",
  cs: "Correct Score",
};

/**
 * The public record. Unauthenticated on purpose — it is the strongest sales
 * asset the product has, and it only works as one if anyone can check it.
 */
export default function Record() {
  const [data, setData] = useState(null);
  const [days, setDays] = useState(30);

  useEffect(() => {
    setData(null);
    api.record(days).then(setData).catch(() => setData({ error: true }));
  }, [days]);

  if (data?.error) return <p className="empty">Couldn't load the record.</p>;

  const summary = data?.summary;
  const settled = summary?.settled ?? 0;

  return (
    <>
      <h2 className="section-title">Our record — last {days} days</h2>

      {!data ? (
        <div className="stat-grid">
          {[0, 1, 2].map((i) => (
            <div className="skeleton" key={i} style={{ height: 78 }} />
          ))}
        </div>
      ) : settled === 0 ? (
        <div className="empty">
          <p>No settled picks yet.</p>
          <p className="muted">The record starts as soon as results come in.</p>
        </div>
      ) : (
        <>
          <div className="stat-grid">
            <div className="stat">
              <b>{summary.settled}</b>
              <small>settled</small>
            </div>
            <div className="stat">
              <b>{summary.win_rate}%</b>
              <small>strike rate</small>
            </div>
            <div
              className={`stat ${summary.roi > 0 ? "positive" : summary.roi < 0 ? "negative" : ""}`}
            >
              <b>{summary.roi === null ? "—" : `${summary.roi > 0 ? "+" : ""}${summary.roi}%`}</b>
              <small>ROI</small>
            </div>
          </div>

          <p className="disclosure">
            ROI is the return on one unit staked level on every published pick at
            the price we quoted. A high strike rate at short odds can still lose
            money — that is why both numbers are here.
          </p>

          {data.by_market.length > 0 && (
            <>
              <h2 className="section-title">By market</h2>
              <div className="card">
                <table className="record">
                  <thead>
                    <tr>
                      <th>Market</th>
                      <th>Picks</th>
                      <th>Won</th>
                      <th>Rate</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.by_market.map((row) => (
                      <tr key={row.market}>
                        <td>{MARKET_LABELS[row.market] ?? row.market}</td>
                        <td>{row.total}</td>
                        <td>{row.won}</td>
                        <td>{row.win_rate}%</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </>
      )}

      <div style={{ marginTop: 16 }}>
        {[7, 30, 90].map((option) => (
          <button
            key={option}
            className={`btn ${option === days ? "primary" : ""}`}
            onClick={() => setDays(option)}
          >
            Last {option} days
          </button>
        ))}
      </div>
    </>
  );
}
