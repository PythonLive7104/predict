import { useEffect, useState } from "react";
import { Route, Routes } from "react-router-dom";
import { loginWithTelegram, restoreSession } from "./api";
import Nav from "./components/Nav";
import Account from "./pages/Account";
import Picks from "./pages/Picks";
import Record from "./pages/Record";
import Slips from "./pages/Slips";
import { initTelegram, isMiniApp } from "./telegram";

export default function App() {
  const [profile, setProfile] = useState(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    initTelegram();
    // Inside Telegram we can sign in silently from initData. On the public web
    // we can only restore a session that already exists — the picks list and the
    // record stay readable either way, so a failure here is not fatal.
    const signIn = isMiniApp() ? loginWithTelegram() : restoreSession();
    signIn
      .then((user) => setProfile(user))
      .catch(() => setProfile(null))
      .finally(() => setReady(true));
  }, []);

  return (
    <div className="app">
      <header className="masthead">
        <div className="wordmark">
          Predict<span>.</span>
        </div>
        {ready && profile && (
          <div className="credit-pill">
            {profile.unlimited ? "Unlimited" : profile.credits}
            <small>{profile.unlimited ? "" : "credits"}</small>
          </div>
        )}
      </header>

      {/*
        Routes wait for the auth attempt to finish. Rendering them earlier lets a
        page fetch before the token exists, so a paying subscriber's first view of
        the slate comes back fully locked and only corrects on a re-navigation.
      */}
      {ready ? (
        <Routes>
          <Route path="/" element={<Picks profile={profile} onProfileChange={setProfile} />} />
          <Route path="/slips" element={<Slips />} />
          <Route path="/record" element={<Record />} />
          <Route path="/account" element={<Account profile={profile} />} />
        </Routes>
      ) : (
        <div className="card">
          <div className="skeleton" style={{ height: 18, width: "60%" }} />
          <div className="skeleton" style={{ height: 12, width: "38%", marginTop: 8 }} />
          <div className="skeleton" style={{ height: 38, marginTop: 14 }} />
        </div>
      )}

      <Nav />
    </div>
  );
}
