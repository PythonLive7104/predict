/**
 * API client.
 *
 * One access token in memory plus a refresh in localStorage. A 401 triggers a
 * single refresh-and-retry; if that fails the caller is logged out rather than
 * left in a loop.
 */
import { initData, isMiniApp } from "./telegram";

const BASE = import.meta.env.VITE_API_BASE ?? "/api";

let accessToken = null;
const REFRESH_KEY = "predict.refresh";

const readRefresh = () => {
  try {
    return localStorage.getItem(REFRESH_KEY);
  } catch {
    return null; // private mode, blocked storage — treat as logged out
  }
};

const writeRefresh = (value) => {
  try {
    value ? localStorage.setItem(REFRESH_KEY, value) : localStorage.removeItem(REFRESH_KEY);
  } catch {
    /* not fatal — the session just won't survive a reload */
  }
};

export const isAuthed = () => Boolean(accessToken);

export function logout() {
  accessToken = null;
  writeRefresh(null);
}

async function request(path, { method = "GET", body, auth = true, retry = true } = {}) {
  const headers = { "Content-Type": "application/json" };
  if (auth && accessToken) headers.Authorization = `Bearer ${accessToken}`;

  const response = await fetch(`${BASE}${path}`, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });

  if (response.status === 401 && auth && retry && (await refresh())) {
    return request(path, { method, body, auth, retry: false });
  }

  const payload = response.status === 204 ? null : await response.json().catch(() => null);
  if (!response.ok) {
    const error = new Error(payload?.detail ?? `Request failed (${response.status})`);
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload;
}

async function refresh() {
  const token = readRefresh();
  if (!token) return false;
  try {
    const data = await request("/auth/refresh/", {
      method: "POST",
      body: { refresh: token },
      auth: false,
      retry: false,
    });
    accessToken = data.access;
    return true;
  } catch {
    logout();
    return false;
  }
}

/** Sign in with Telegram initData. Returns the profile, or null outside Telegram. */
export async function loginWithTelegram() {
  if (!isMiniApp()) return null;
  const data = await request("/auth/telegram/", {
    method: "POST",
    body: { init_data: initData() },
    auth: false,
  });
  accessToken = data.access;
  writeRefresh(data.refresh);
  return data.user;
}

/** Restore a session from a stored refresh token on a cold load. */
export async function restoreSession() {
  if (!(await refresh())) return null;
  try {
    return await api.me();
  } catch {
    logout();
    return null;
  }
}

export const api = {
  picks: (date) => request(`/picks/today/${date ? `?date=${date}` : ""}`, { auth: true }),
  unlock: (id) => request(`/picks/${id}/unlock/`, { method: "POST" }),
  slips: () => request("/slips/today/"),
  record: (days = 30) => request(`/record/?days=${days}`),
  plans: () => request("/plans/", { auth: false }),
  checkout: (plan) => request("/checkout/", { method: "POST", body: { plan } }),
  credits: () => request("/credits/"),
  me: () => request("/me/"),
};
