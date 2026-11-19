# Sandboxed sessions

Sentinel Bank fraud operations, internal policy (demo document written for this project).

## Why we sandbox
When a session shows account-takeover signals (a SIM swap or failed device integrity on a new device, with a high score), the session is sandboxed instead of blocked. The person controlling it sees payments as "Allowed" and a falling balance, but nothing is sent. This keeps them from moving to another channel while we learn which payees they use.

## What the analyst should do
Check the sandboxed session in Fraud ops: how many attempts, how much, how many payees. Each payee it tried is filed to the consortium ledger as a sandbox capture. End the session once the attempts stop or you have enough, then contact the real customer on their registered number from a separate channel.

## Sandbox captures on the ledger
Sandbox captures are filed at LOW severity and weigh 0 on their own. They cannot get a payee flagged across the consortium; they only corroborate reports from other banks. This stops one bank's honeypot from harming innocent payees.

## The real customer
The real customer, signing in on their own device, sees the true balance and a message that someone else was using their account. Help them secure the account: new passkey, check the SIM with the telecom operator, and review recent payees.
