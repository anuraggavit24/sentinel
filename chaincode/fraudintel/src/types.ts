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
