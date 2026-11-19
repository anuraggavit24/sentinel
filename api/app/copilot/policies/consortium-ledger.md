# Consortium fraud ledger rules

Sentinel Bank fraud operations, internal policy (demo document written for this project).

## What the ledger is
A Hyperledger Fabric channel shared by three banks and a regulator. It holds mule reports about payees. A payee is identified only by a keyed hash of its normalised account or UPI ID, never the identifier itself. Each report also carries the hash of the reporting bank's case record as evidence.

## Report states
A report starts PENDING. It becomes CONFIRMED when two different banks with non-sandbox evidence stand behind it. A bank can dispute a report; the regulator resolves disputes as UPHELD or OVERTURNED. The reporting bank can retract its own report.

## Weights and what they do to a payment
A report only from one bank's sandbox weighs 0 and adds nothing. A pending report from one real bank weighs 0.25 and adds 10 risk points. A report disputed after being confirmed weighs 0.5 and adds 20 points. A CONFIRMED report weighs 1, adds 50 points, and the payment is always held for review, whatever its score.

## Reporting and confirming
When an analyst rejects and reports, the system first asks the ledger what it already holds. If nobody has an active report, it files a new one. If another bank has one, it confirms that report. If we already have one, it writes nothing. A bank cannot confirm its own report.

## When the ledger is unavailable
If the ledger does not answer within one second, the payment is scored without it and the analyst view shows "ledger unavailable". Treat such a payment as if the ledger answer were unknown, not as clear. Reports that could not be filed are retried automatically.

## What customers see
Customers see one sentence for any report, "Banks have received fraud reports about this payee", never the status, the weight or which bank reported it.
