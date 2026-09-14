import { useEffect, useRef } from "react";
import { botUsername } from "../telegram-link";

/**
 * Telegram's official Login Widget.
 *
 * It is an injected <script> rather than a React component because Telegram
 * renders the button itself in an iframe — there is no API to drive it, and the
 * callback is reached through a global the script looks for by name.
 *
 * Two things must line up for it to appear at all, and when either is wrong the
 * widget renders nothing with no error in the console:
 *   1. VITE_TELEGRAM_BOT_USERNAME is baked into this build.
 *   2. The bot has this exact domain registered — in Telegram, message
 *      @BotFather, /setdomain, choose the bot, send crazybetpredictions.info
 */
export default function TelegramLogin({ onAuth, onError }) {
  const holder = useRef(null);
  const name = botUsername();

  useEffect(() => {
    if (!name || !holder.current) return;

    // The widget calls a global by name. Scoped to this mount and cleaned up on
    // unmount so a remount cannot leave a stale closure holding an old handler.
    const callbackName = "onTelegramAuth";
    window[callbackName] = (user) => {
      Promise.resolve(onAuth(user)).catch((err) => onError?.(err));
    };

    const script = document.createElement("script");
    script.src = "https://telegram.org/js/telegram-widget.js?22";
    script.async = true;
    script.setAttribute("data-telegram-login", name);
    script.setAttribute("data-size", "large");
    script.setAttribute("data-radius", "8");
    script.setAttribute("data-userpic", "false");
    script.setAttribute("data-request-access", "write");
    script.setAttribute("data-onauth", `${callbackName}(user)`);

    holder.current.appendChild(script);
    return () => {
      delete window[callbackName];
      if (holder.current) holder.current.innerHTML = "";
    };
  }, [name, onAuth, onError]);

  if (!name) {
    return (
      <p className="detail" style={{ marginBottom: 0 }}>
        Telegram sign-in isn’t configured yet. Search for the bot in Telegram and
        tap Start.
      </p>
    );
  }
  return <div ref={holder} />;
}
