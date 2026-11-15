import {
  startAuthentication,
  startRegistration,
  type PublicKeyCredentialCreationOptionsJSON,
  type PublicKeyCredentialRequestOptionsJSON,
} from "@simplewebauthn/browser";

export interface Device {
  id: string;
  label: string;
  deviceType: string;
  synced: boolean;
  createdAt: string;
  lastUsedAt: string | null;
}

export interface Session {
  id: string;
  current: boolean;
  userAgent: string | null;
  ip: string | null;
  createdAt: string;
  expiresAt: string;
}

export interface Me {
  username: string;
  displayName: string;
  devices: Device[];
  sessions: Session[];
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, detail);
  }
  return res.json() as Promise<T>;
}

const post = <T>(path: string, body?: unknown) =>
  call<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

interface Ceremony<O> {
  challengeId: string;
  options: O;
}

export async function registerWithPasskey(username: string, displayName: string, label: string) {
  const { challengeId, options } = await post<Ceremony<PublicKeyCredentialCreationOptionsJSON>>(
    "/auth/register/options",
    { username, displayName },
  );
  const credential = await startRegistration({ optionsJSON: options });
  return post("/auth/register/verify", { challengeId, credential, label });
}

export async function signInWithPasskey(username: string) {
  const { challengeId, options } = await post<Ceremony<PublicKeyCredentialRequestOptionsJSON>>(
    "/auth/login/options",
    { username },
  );
  const credential = await startAuthentication({ optionsJSON: options });
  return post("/auth/login/verify", { challengeId, credential });
}

export async function addDevice(label: string) {
  const { challengeId, options } = await post<Ceremony<PublicKeyCredentialCreationOptionsJSON>>("/me/devices/options");
  const credential = await startRegistration({ optionsJSON: options });
  return post("/me/devices/verify", { challengeId, credential, label });
}

export const getMe = () => call<Me>("/me");
export const signOut = () => post("/auth/logout");
export const removeDevice = (id: string) => call(`/me/devices/${encodeURIComponent(id)}`, { method: "DELETE" });
export const revokeSession = (id: string) => call(`/me/sessions/${encodeURIComponent(id)}`, { method: "DELETE" });

/** Turns browser WebAuthn errors into plain messages. */
export function explain(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error) {
    if (err.name === "NotAllowedError") return "The passkey prompt was cancelled or timed out.";
    if (err.name === "InvalidStateError") return "This device already has a passkey for this account.";
    return err.message;
  }
  return String(err);
}

// ---------------------------------------------------------------- Module 2: payments

export type PayeeType = "ACCOUNT" | "UPI" | "PHONE";
export type Band = "allow" | "step_up" | "sandbox";

export interface Reason {
  code: string;
  points: number;
  text: string;
  group: "TRANSACTION" | "DEVICE" | "COACHING" | "TELECOM";
}

export interface Decision {
  paymentId: string;
  status: "completed" | "held";
  payee: string;
  amount: string;
  score: number;
  band: Band;
  reasons: Reason[];
  takeoverSignals: boolean;
  coachingSignals: boolean;
  engine: string;
  signals: { consortium: string; friCategory: string | null; injected: DemoSignals | null };
}

export interface PaymentRow {
  id: string;
  payeeType: PayeeType;
  payee: string;
  amount: string;
  score: number;
  band: Band;
  status: string;
  reasonCodes: string[];
  createdAt: string;
}

export interface DemoSignals {
  simSwapHoursAgo: number | null;
  newDevice: boolean;
  integrity: "PASS" | "FAIL" | "UNKNOWN";
  activeCall: boolean;
  screenSharing: boolean;
  accessibilityServices: boolean;
}

export const scorePayment = (payeeType: PayeeType, payee: Record<string, string>, amount: string) =>
  post<Decision>("/payments/score", { payeeType, payee, amount });
export const listPayments = () => call<{ payments: PaymentRow[] }>("/payments");
export const getSignals = () => call<DemoSignals>("/demo/signals");
export const putSignals = (s: DemoSignals) => call<DemoSignals>("/demo/signals", { method: "PUT", body: JSON.stringify(s) });
export const getDemoFri = () => call<{ mock: boolean; numbers: Record<string, string> }>("/demo/fri");
