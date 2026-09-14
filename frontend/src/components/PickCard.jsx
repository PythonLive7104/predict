import { useState } from "react";
import { tap } from "../telegram";

const kickoffTime = (iso) =>
  new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

/**
 * A pick, locked or open.
 *
 * A locked card shows the fixture and nothing else — no market, no confidence,
 * no selection — because the API never sent any of it. The gate is server-side,
 * so nothing here is hidden with CSS and devtools reveals nothing.
 */
export default function PickCard({ pick, onUnlock, canUnlock }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const { fixture } = pick;

  async function unlock() {
    setBusy(true);
    setError(null);
    tap();
    try {
      await onUnlock(pick.id);
    } catch (err) {
      setError(
        err.status === 402
          ? "You're out of credits."
          : err.message ?? "Something went wrong.",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <article className="card">
      <div className="pick-head">
        <div>
          <div className="teams">
            {fixture.home} <span className="muted">v</span> {fixture.away}
          </div>
          <div className="meta">
            {fixture.league} · {kickoffTime(fixture.kickoff)}
            {" · "}
            <span className={`tag ${pick.tier}`}>{pick.tier}</span>
            {pick.outcome !== "pending" && (
              <>
                {" "}
                <span className={`tag ${pick.outcome}`}>{pick.outcome}</span>
              </>
            )}
          </div>
        </div>
        {pick.unlocked && (
          <div className="confidence">
            <b>{pick.confidence}%</b>
            <small>confidence</small>
          </div>
        )}
      </div>

      {/* The meter would otherwise render at width 0% on every locked card — an
          empty bar reads as "no confidence" rather than "not shown yet". */}
      {pick.unlocked && (
        <div className="meter">
          <i style={{ width: `${pick.confidence}%` }} />
        </div>
      )}

      <div className="market-row">
        {pick.unlocked ? (
          <>
            <span className="label">{pick.market_label}</span>
            <span className="selection">
              {pick.selection_label}
              {pick.market_odds && <span className="odds"> @ {pick.market_odds}</span>}
            </span>
          </>
        ) : (
          <span className="muted">🔒 Analysis locked</span>
        )}
      </div>

      {pick.unlocked && pick.rationale && <p className="rationale">{pick.rationale}</p>}

      {!pick.unlocked && (
        <>
          <button
            className="btn primary"
            style={{ marginTop: 12 }}
            onClick={unlock}
            disabled={busy || !canUnlock}
          >
            {busy ? "Unlocking…" : canUnlock ? "Unlock · 1 credit" : "Sign in to unlock"}
          </button>
          {error && <p className="caveat">{error}</p>}
        </>
      )}
    </article>
  );
}
