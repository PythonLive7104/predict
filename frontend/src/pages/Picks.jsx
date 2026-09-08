import { useEffect, useState } from "react";
import { api } from "../api";
import Disclosure from "../components/Disclosure";
import PickCard from "../components/PickCard";

export default function Picks({ profile, onProfileChange }) {
  const [picks, setPicks] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    api
      .picks()
      .then((data) => setPicks(data.results))
      .catch((err) => setError(err.message));
  }, []);

  async function unlock(id) {
    const result = await api.unlock(id);
    setPicks((current) =>
      current.map((pick) => (pick.id === id ? result.prediction : pick)),
    );
    onProfileChange({ ...profile, credits: result.credits });
  }

  if (error) return <p className="empty">{error}</p>;
  if (!picks) return <Loading />;

  if (picks.length === 0) {
    return (
      <div className="empty">
        <p>No picks published yet.</p>
        <p className="muted">
          The model runs a few hours before the first kickoff, once team news lands.
        </p>
      </div>
    );
  }

  const free = picks.filter((p) => p.tier === "free");
  const vip = picks.filter((p) => p.tier !== "free");

  return (
    <>
      {free.length > 0 && (
        <>
          <h2 className="section-title">Free picks today</h2>
          {free.map((pick) => (
            <PickCard key={pick.id} pick={pick} onUnlock={unlock} canUnlock={Boolean(profile)} />
          ))}
        </>
      )}

      {vip.length > 0 && (
        <>
          <h2 className="section-title">More analysis</h2>
          {vip.map((pick) => (
            <PickCard key={pick.id} pick={pick} onUnlock={unlock} canUnlock={Boolean(profile)} />
          ))}
        </>
      )}

      <Disclosure />
    </>
  );
}

function Loading() {
  return (
    <>
      <h2 className="section-title">Today</h2>
      {[0, 1, 2].map((i) => (
        <div className="card" key={i}>
          <div className="skeleton" style={{ height: 18, width: "62%" }} />
          <div className="skeleton" style={{ height: 12, width: "40%", marginTop: 8 }} />
          <div className="skeleton" style={{ height: 4, marginTop: 14 }} />
          <div className="skeleton" style={{ height: 38, marginTop: 14 }} />
        </div>
      ))}
    </>
  );
}
