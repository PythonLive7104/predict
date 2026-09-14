/**
 * Where to send a visitor who is on the public web app rather than inside
 * Telegram.
 *
 * The whole product signs in through Telegram — there is no password, no email,
 * no separate account. So a signed-out visitor is not "logged out", they are in
 * the wrong place, and the only useful thing the page can do is hand them a door.
 */
const USERNAME = import.meta.env.VITE_TELEGRAM_BOT_USERNAME ?? "";

export const botUsername = () => USERNAME.replace(/^@/, "");

/** Deep link to the bot, carrying a referral code when one is present. */
export function botLink(startParam = "") {
  const name = botUsername();
  if (!name) return "";
  return startParam
    ? `https://t.me/${name}?start=${encodeURIComponent(startParam)}`
    : `https://t.me/${name}`;
}
