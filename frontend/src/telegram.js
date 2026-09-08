/**
 * Telegram Mini App bridge.
 *
 * The same build runs at the public domain and inside Telegram. `initData` is
 * non-empty only in the Telegram webview, so it doubles as the environment
 * check and the credential — there is no separate "am I in Telegram" flag to
 * keep in sync.
 */
const tg = () => window.Telegram?.WebApp;

export const isMiniApp = () => Boolean(tg()?.initData);

export function initTelegram() {
  const app = tg();
  if (!app) return;
  app.ready();
  app.expand();

  // Only stamp a theme when we are genuinely inside Telegram. The bridge script
  // defines window.Telegram.WebApp on any page that loads it and reports
  // colorScheme "light" outside the client — stamping that on the public web app
  // would pin it to light and override the visitor's own system preference.
  if (!isMiniApp()) return;
  document.documentElement.dataset.theme = app.colorScheme === "dark" ? "dark" : "light";
  app.onEvent?.("themeChanged", () => {
    document.documentElement.dataset.theme =
      app.colorScheme === "dark" ? "dark" : "light";
  });
}

export const initData = () => tg()?.initData ?? "";

/** Haptics where the platform offers them; a no-op in a browser. */
export function tap(style = "light") {
  tg()?.HapticFeedback?.impactOccurred?.(style);
}

/** Telegram supplies its own back button; using it beats drawing our own. */
export function setBackButton(onClick) {
  const app = tg();
  if (!app?.BackButton) return () => {};
  if (onClick) {
    app.BackButton.show();
    app.BackButton.onClick(onClick);
    return () => {
      app.BackButton.offClick(onClick);
      app.BackButton.hide();
    };
  }
  app.BackButton.hide();
  return () => {};
}
