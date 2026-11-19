export const BANK_MSPS: readonly string[] = ['BankAMSP', 'BankBMSP', 'BankCMSP'];
export const REGULATOR_MSP = 'RegulatorMSP';
export const ALL_MSPS: readonly string[] = [...BANK_MSPS, REGULATOR_MSP];

export type IdType = 'ACCOUNT' | 'UPI' | 'PHONE';
export type Source = 'SANDBOX_CAPTURE' | 'ANALYST' | 'CUSTOMER_COMPLAINT';
export type Severity = 'LOW' | 'MEDIUM' | 'HIGH';
export type Status = 'PENDING' | 'CONFIRMED' | 'DISPUTED' | 'OVERTURNED' | 'RETRACTED';
export type Outcome = 'UPHELD' | 'OVERTURNED';

export const ID_TYPES: readonly IdType[] = ['ACCOUNT', 'UPI', 'PHONE'];
export const SOURCES: readonly Source[] = ['SANDBOX_CAPTURE', 'ANALYST', 'CUSTOMER_COMPLAINT'];
export const SEVERITIES: readonly Severity[] = ['LOW', 'MEDIUM', 'HIGH'];
export const OUTCOMES: readonly Outcome[] = ['UPHELD', 'OVERTURNED'];

export interface Confirmation {
  org: string;
  source: Source;
  evidenceHash: string;
  txTime: string;
}

export interface Dispute {
  org: string;
  reasonHash: string;
  txTime: string;
}

export interface MuleReport {
  docType: 'report';
  reportId: string;
  idHash: string;
  idType: IdType;
  reporterOrg: string;
  evidenceHash: string;
  source: Source;
  severity: Severity;
  confirmations: Confirmation[];
  disputes: Dispute[];
  status: Status;
  statusBeforeDispute: Status | null;
  resolution: { outcome: Outcome; txTime: string } | null;
  keyVersion: number;
  createdAt: string;
  updatedAt: string;
}

export interface AuditAnchor {
  docType: 'anchor';
  org: string;
  date: string;
  rootHash: string;
  txTime: string;
}

export interface WeightedReport {
  reportId: string;
  idHash: string;
  status: Status;
  source: Source;
  severity: Severity;
  reporterOrg: string;
  keyVersion: number;
  nonSandboxBanks: number;
  weight: number;
  hardRuleEligible: boolean;
}

// ---------- payments (demo bank network) ----------

export type PaymentVerdict = 'ALLOWED' | 'WARNED';
export const PAYMENT_VERDICTS: readonly PaymentVerdict[] = ['ALLOWED', 'WARNED'];

/** Held only by the two banks in the payment (a private data collection). Never on the shared channel. */
export interface PaymentPrivate {
  paymentId: string;
  fromBank: string;
  toBank: string;
  payer: string;        // payer's UPI ID
  payerName: string;
  payee: string;        // payee's UPI ID
  amountPaise: number;
  verdict: PaymentVerdict;
  createdAt: string;    // when the sending bank accepted it
  salt: string;         // 32 random bytes, hex: stops anyone guessing the details from the public hash
}

/** On the shared channel, readable by every member: who was involved and a hash of the details. */
export interface PaymentPublic {
  docType: 'payment';
  paymentId: string;
  fromBank: string;
  toBank: string;
  collection: string;   // where the details live
  dataHash: string;     // SHA-256 of the private record's exact bytes
  txId: string;
  txTime: string;
}
