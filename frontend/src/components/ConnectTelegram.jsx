import { useEffect, useRef, useState } from "react";
import { pollConnect, startConnect } from "../api";

const POLL_MS = 2000;

/**
 * Connect this browser to a Telegram account.
 *
 * Deliberately not Telegram's Login Widget. That signs you into *Telegram* —
 * phone number, confirmation code — which is a lot to ask of someone who has
 * not bought anything, and it is not what people think they are doing. Here you
 * open the bot and tap Start, which is the thing you already came to do; the
 * page notices and signs you in.
 *
 * The code is single-use and short-lived, so an abandoned attempt leaves nothing
 * claimable behind.
 */
export default function ConnectTelegram({ onConnected }) {
  const [state, setState] = useState("idle"); // idle | waiting | expired | error
  const timer = useRef(null);

  useEffect(() => () => clearInterval(timer.current), []);

  async function connect() {
    setState("waiting");
    let session;
    try {
      session = await startConnect();
    } catch {
      setState("error");
      return;
    }

    if (!session.bot_url) {
      setState("error");
      return;
    }

    // Opened before polling starts so the click is still what opened it —
    // browsers block a window opened from an async callback.
    window.open(session.bot_url, "_blank", "noopener");

    clearInterval(timer.current);
    timer.current = setInterval(async () => {
      try {
        const profile = await pollConnect(session.code);
        if (!profile) return; // still waiting on the Start tap
        clearInterval(timer.current);
        onConnected(profile);
      } catch (err) {
        // 404 covers unknown, expired and already-spent alike — the server does
        // not distinguish them, so neither can we.
        clearInterval(timer.current);
        setState(err.status === 404 ? "expired" : "error");
      }
    }, POLL_MS);
  }

  if (state === "waiting") {
    return (
      <div>
        <p className="muted" style={{ marginTop: 0 }}>
          Waiting for Telegram… tap <strong>Start</strong> in the chat that just
          opened, then come back here. This page signs you in on its own.
        </p>
        <button className="btn" onClick={connect}>
          Open Telegram again
        </button>
      </div>
    );
  }

  return (
    <div>
      <button className="btn primary" onClick={connect}>
        Connect Telegram
      </button>
      {state === "expired" && (
        <p className="caveat">That link expired. Tap Connect to get a fresh one.</p>
      )}
      {state === "error" && (
        <p className="caveat">Couldn’t start the connection. Please try again.</p>
      )}
    </div>
  );
}
