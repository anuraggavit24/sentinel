# Sentinel: consortium fraud-intelligence ledger

> **Before `fablo up`:** `cd chaincode/fraudintel && npm install && npm run build`
> `dist/` is git-ignored, so a fresh clone must build first.
> Run everything inside WSL2, not PowerShell.

Hyperledger Fabric chaincode (TypeScript) for sharing mule-account reports between banks,
with a 2-bank non-sandbox confirmation rule and a Regulator that can only rule on disputes.

## Tests

```bash
cd chaincode/fraudintel
npm test
```

23 tests, including attack tests:
- two-sandbox poisoning
- one bank plus a sandbox capture
- a dispute escalated through the Regulator
- partial writes from failed transactions

The test stub mirrors Fabric's commit model: reads never see uncommitted writes,
and a failed transaction leaves no writes behind.

## Scenario 4 on the live network

```bash
cd ~/sentinel
fablo up                      # first time, or after fablo down
./scripts/scenario4.sh        # report, confirm, poisoning, disputes, referee limits, chaos test
```

Uses Fabric 2.5.16 (2.5.15+ is required for Docker Engine 29).
