import { useEffect, useState } from "react";
import { api } from "../api";
import { botLink } from "../telegram-link";
import ConnectTelegram from "../components/ConnectTelegram";
import { isMiniApp, tap } from "../telegram";

const money = (value) => {
  const number = Number(value);
  return Number.isInteger(number) ? `$${number}` : `$${number.toFixed(2)}`;
};

export default function Account({ profile, onProfileChange }) {
  const [plans, setPlans] = useState([]);
  const [busy, setBusy] = useState(null);
  const [error, setError] = useState(null);
  const [syncing, setSyncing] = useState(false);

  /**
   * Pull the account again on demand.
   *
   * The app already re-reads on focus, but someone who buys a plan in the bot
   * while this tab is open and visible on another screen sees nothing change.
   * A button costs little and removes the "is it broken?" moment.
   */
  async function resync() {
    setSyncing(true);
    try {
      onProfileChange?.(await api.me());
    } catch {
      /* leave the existing profile in place rather than blanking the card */
    } finally {
      setSyncing(false);
    }
  }


  useEffect(() => {
    api.plans().then(setPlans).catch(() => setPlans([]));
  }, []);

  async function buy(code) {
    setBusy(code);
    setError(null);
    tap("medium");
    try {
      const payment = await api.checkout(code);
      // The invoice is hosted by the payment provider. Inside Telegram, openLink
      // hands off to the in-app browser rather than dropping the Mini App.
      if (isMiniApp()) window.Telegram.WebApp.openLink(payment.invoice_url);
      else window.location.href = payment.invoice_url;
    } catch (err) {
      setError(err.message ?? "Could not open a payment window.");
    } finally {
      setBusy(null);
    }
  }

  return (
    <>
      <h2 className="section-title">Your plan</h2>
      <div className="card">
        {profile ? (
          <>
            <div className="plan">
              <div>
                <div className="teams">{profile.plan}</div>
                <div className="detail">
                  {profile.unlimited ? "Unlimited picks" : `${profile.credits} credits`}
                  {profile.vip && " · VIP slips"}
                </div>
              </div>
            </div>
            <p className="detail" style={{ marginTop: 12, marginBottom: 0 }}>
              ✅ Connected to Telegram
              {profile.username ? (
                <> as <strong>@{profile.username}</strong></>
              ) : null}
              {" — "}the same account the bot uses, so credits and plans match on
              both.
            </p>
            {profile.referral_code && (
              <p className="detail" style={{ marginTop: 8, marginBottom: 0 }}>
                Referral code: <strong>{profile.referral_code}</strong> — both of you
                get credits when a friend joins with it.
              </p>
            )}
            <button
              className="btn"
              style={{ marginTop: 12 }}
              onClick={resync}
              disabled={syncing}
            >
              {syncing ? "Refreshing…" : "Refresh from Telegram"}
            </button>
          </>
        ) : (
          <div className="signin">
            <p className="muted" style={{ marginTop: 0 }}>
              Connect your Telegram account — picks, credits and plans live there,
              and this links you to the same one. No password, no phone number.
            </p>
            <ConnectTelegram onConnected={() => window.location.reload()} />
            {botLink() && (
              <p className="detail" style={{ marginBottom: 0, marginTop: 12 }}>
                Or <a href={botLink()} target="_blank" rel="noreferrer">open the bot
                directly</a>.
              </p>
            )}
          </div>
        )}
      </div>

      <h2 className="section-title">Plans</h2>
      {plans.map((plan) => (
        <article className="card" key={plan.code}>
          <div className="plan">
            <div>
              <div className="teams">{plan.name}</div>
              <div className="detail">
                {plan.unlimited ? "Unlimited picks" : `${plan.credits_granted} credits`}
                {plan.includes_vip_slips && " · VIP slips"}
              </div>
            </div>
            <div className="price">{money(plan.price_usd)}</div>
          </div>
          <button
            className="btn primary"
            style={{ marginTop: 12 }}
            disabled={!profile || busy === plan.code}
            onClick={() => buy(plan.code)}
          >
            {busy === plan.code ? "Opening…" : "Pay with crypto"}
          </button>
        </article>
      ))}

      {error && <p className="caveat">{error}</p>}

      <p className="disclosure">
        Plans are paid in crypto and activate automatically once the payment
        confirms on-chain. Access is to analysis only — we take no bets and hold
        no stakes.
      </p>
    </>
  );
}
