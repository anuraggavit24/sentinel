# Held payment review playbook

Sentinel Bank fraud operations, internal policy (demo document written for this project).

## When a payment is held
A payment is held for review when its risk score reaches 40 or more, when a customer described a scam pattern in the step-up questions, or when the payee is CONFIRMED on the consortium ledger. A held payment has not left the bank. The analyst decides to release it, reject it, or reject it and report the payee to the consortium.

## Order of work
Review flagged step-ups first: a customer who answered that someone on a call asked them to pay, or that they were promised a refund, prize or job, is the most likely victim in progress. Then review payments whose payee is CONFIRMED on the ledger, then the oldest held payments.

## When to release
Release only when the reasons that held the payment have an innocent explanation that you have checked. Examples: a large first payment to a new payee that the customer confirmed on a call back to their registered number, with no coaching signals and no ledger reports. Releasing does not make the payee trusted: trust still needs completed payments over time.

## When to reject
Reject when the payee is CONFIRMED on the consortium ledger, when coaching signals (an active call or screen sharing during the payment) match a scam pattern, when the customer described a scam in the step-up, or when the customer cannot be reached and the amount is large. Rejecting stops the payment; no money moves.

## When to report to the consortium
Report the payee when you have evidence of your own: the customer confirmed they were deceived, the payee matches a known mule pattern, or several of our customers tried to pay the same payee. Do not report on suspicion alone. If another bank already reported the payee, the system adds our confirmation instead of a second report.

## Never do this
Never ask a customer for an OTP, PIN, password or passkey. Never release a payment because the customer is angry or in a hurry: urgency is the scammer's main tool. Never call a number given in a message or by the payee; call the customer's registered number only.
