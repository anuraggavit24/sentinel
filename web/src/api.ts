import type { BehaviourSample } from "./behaviour";
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
  role: "customer" | "analyst";
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
  text: string;
  group: "TRANSACTION" | "DEVICE" | "COACHING" | "TELECOM";
}

export interface AnalystReason extends Reason {
  points: number;
}

/** Slice 2c: what the consortium ledger said about this payee. */
export interface ConsortiumView {
  status: "disabled" | "clear" | "hit" | "unavailable";
  maxWeight?: number;
  hardRule?: boolean;
  latencyMs?: number;
  error?: string;
  reports?: {
    reportId: string;
    status: string;
    severity: string;
    source: string;
    reporterOrg: string;
    nonSandboxBanks: number;
    weight: number;
  }[];
}

/** Only present when the server runs with SHOW_ANALYST_VIEW=1 (demo / fraud-ops). */
export interface AnalystView {
  score: number;
  reasons: AnalystReason[];
  takeoverSignals: boolean;
  coachingSignals: boolean;
  signals: {
    consortium: ConsortiumView;
    friCategory: string | null;
    injected: DemoSignals | null;
    operatorSignalsOn: boolean;
    behaviour: {
      model: string;
      samples: number;
      anomaly: number | null;
      enrolMin: number;
      note: string | null;
      captured: boolean;
      learned: boolean;
      notLearnedBecause: string | null;
    };
  };
}

export interface Decision {
  paymentId: string;
  status: "completed" | "held";
  payee: string;
  amount: string;
  band: Band;
  reasons: Reason[];
  engine: string;
  replayed: boolean;
  analyst?: AnalystView;
}

export interface PaymentRow {
  id: string;
  payeeType: PayeeType;
  payee: string;
  amount: string;
  score?: number;
  band: Band;
  status: string;
  reviewed: "release" | "reject" | null;
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

/** idempotencyKey: one per payment attempt. Reuse it when retrying the same payment so it is never sent twice. */
export const scorePayment = (
  payeeType: PayeeType,
  payee: Record<string, string>,
  amount: string,
  idempotencyKey: string,
  behaviour: BehaviourSample | null,
) =>
  call<Decision>("/payments/score", {
    method: "POST",
    body: JSON.stringify({ payeeType, payee, amount, behaviour }),
    headers: { "Content-Type": "application/json", "Idempotency-Key": idempotencyKey },
  });
export const listPayments = () => call<{ payments: PaymentRow[] }>("/payments");

const operatorHeaders = (token: string) => ({ "Content-Type": "application/json", "x-demo-operator-token": token });
export const getSignals = (sessionId: string, token: string) =>
  call<DemoSignals>(`/demo/sessions/${sessionId}/signals`, { headers: operatorHeaders(token) });
export const putSignals = (sessionId: string, token: string, s: DemoSignals) =>
  call<DemoSignals>(`/demo/sessions/${sessionId}/signals`, { method: "PUT", body: JSON.stringify(s), headers: operatorHeaders(token) });
export const getDemoFri = (token: string) =>
  call<{ mock: boolean; numbers: Record<string, string> }>("/demo/fri", { headers: operatorHeaders(token) });

// ---------------------------------------------------------------- Module 2b: guided behaviour enrolment

export interface BehaviourProfile {
  samples: number;
  enrolMin: number;
  enrolled: boolean;
  enrolment: { added: number; needed: number; expiresAt: string } | null;
}

export const getBehaviourProfile = () => call<BehaviourProfile>("/behaviour/profile");

/** Opens a 10-minute enrolment window after a fresh passkey check (Windows Hello prompt). */
export async function startEnrolment(reset: boolean) {
  const { challengeId, options } = await post<Ceremony<PublicKeyCredentialRequestOptionsJSON>>("/behaviour/enrol/options");
  const credential = await startAuthentication({ optionsJSON: options });
  return post<{ open: boolean; needed: number; minutes: number }>("/behaviour/enrol/start", { challengeId, credential, reset });
}

export const sendEnrolmentSample = (behaviour: BehaviourSample | null) =>
  post<{ added: number; needed: number; done: boolean }>("/behaviour/enrol/sample", { behaviour });

// ---------------------------------------------------------------- Module 2b part 2: volunteer typing study
// Not linked to bank accounts. The participant code is sent as a header and kept in memory only: the
// study runs on a shared laptop, so storing it would hand the next volunteer someone else's session.
export interface StudyProtocol {
  consentVersion: string;
  consent: string[];
  prompts: { upi: string; amount: string }[];
  samplesPerSession: number;
  minSessions: number;
  maxSessions: number;
  sessionGapHours: number;
}

export interface StudyStatus {
  keyboard?: string;
  sessions: { session: number; samples: number }[];
  next: { session: number | null; promptNo: number | null; allowed: boolean; opensAt: string | null; finished: boolean };
  samplesPerSession: number;
  minSessions: number;
  maxSessions: number;
  enoughForStudy: boolean;
}

const study = <T>(path: string, code: string | null, body?: unknown) =>
  call<T>(path, {
    method: body === undefined ? "GET" : "POST",
    headers: { "Content-Type": "application/json", ...(code ? { "X-Participant-Code": code } : {}) },
    body: body === undefined ? undefined : JSON.stringify(body),
  });

export const getStudyProtocol = () => study<StudyProtocol>("/research/protocol", null);
export const joinStudy = (inviteCode: string, consentVersion: string, keyboard: string) =>
  study<{ participantCode: string }>("/research/join", null, { inviteCode, consentVersion, keyboard });
export const getStudyStatus = (code: string) => study<StudyStatus>("/research/me", code);
export const sendStudySample = (code: string, promptNo: number, behaviour: BehaviourSample | null) =>
  study<StudyStatus>("/research/samples", code, { promptNo, behaviour });
export const withdrawFromStudy = (code: string) =>
  study<{ withdrawn: boolean; samplesDeleted: number }>("/research/withdraw", code, {});

// ---------------------------------------------------------------- Module 3a: fraud operations
export interface OpsStatus {
  elevatedUntil: string | null;
  ledgerWrites: boolean;
  bank: string;
}

export interface QueueItem {
  id: string;
  createdAt: string;
  amount: string;
  payeeType: PayeeType;
  payee: string;
  payeeRef: string;
  customer: string;
  score: number;
  band: Band;
  reasons: AnalystReason[];
  coaching: boolean;
  consortium: ConsortiumView | null;
  otherCustomers7d: number;
}

export interface Filing {
  id: number;
  status: "pending" | "reported" | "confirmed" | "already_ours" | "failed";
  attempts: number;
  lastError: string | null;
  reportId: string | null;
  severity: string;
  payee: string | null;
  amount: string | null;
  evidenceHash: string;
  createdAt: string;
  doneAt: string | null;
  nextAttemptAt: string | null;
}

export const getOpsStatus = () => call<OpsStatus>("/ops/status");
export const getQueue = () => call<{ payments: QueueItem[] }>("/ops/queue");
export const getFilings = () => call<{ filings: Filing[] }>("/ops/filings");

/** Analyst actions need a fresh passkey check (Windows Hello prompt); it lasts a few minutes. */
export async function elevateOps() {
  const { challengeId, options } = await post<Ceremony<PublicKeyCredentialRequestOptionsJSON>>("/ops/elevate/options");
  const credential = await startAuthentication({ optionsJSON: options });
  return post<{ elevatedUntil: string; minutes: number }>("/ops/elevate", { challengeId, credential });
}

export const decidePayment = (
  id: string,
  body: { decision: "release" | "reject"; note: string; report: boolean; severity: "LOW" | "MEDIUM" | "HIGH" },
) => post<{ status: string; reported: boolean; outboxId: number | null }>(`/ops/payments/${id}/decision`, body);
