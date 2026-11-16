// Browser test for Module 3a: a customer's payments are held; an analyst (granted by the operator)
// verifies with a passkey, rejects one and reports it to the consortium ledger, releases the other;
// the customer sees both outcomes. Two separate browser contexts = two people, two passkeys.
// Run with the stack up (and Fablo for the ledger part):
//   node e2e/ops.e2e.mjs
import { chromium } from "playwright";
const BASE = process.env.BASE_URL ?? "http://localhost:5173";
const API = process.env.API_URL ?? "http://localhost:8000";
const OPERATOR = process.env.OPERATOR_TOKEN ?? "demo-operator-token-change-me";
const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || undefined, args: ["--no-sandbox"] });
let failures = 0;
const ok = (cond, msg) => { console.log(cond ? "✔" : "✘", msg); if (!cond) failures++; };
const run = Date.now().toString(36);
// Amounts unique to this run, so leftovers from earlier runs in the same database never get picked.
const n = 100 + (Date.now() % 800);
const MULE_AMT = 60000 + n, OK_AMT = 55000 + n;
const inr = (v) => "₹" + v.toLocaleString("en-IN");

async function person(name) {
  const context = await browser.newContext({ viewport: { width: 1100, height: 1400 } });
  const page = await context.newPage();
  const cdp = await context.newCDPSession(page);
  await cdp.send("WebAuthn.enable");
  await cdp.send("WebAuthn.addVirtualAuthenticator", { options: {
    protocol: "ctap2", transport: "internal", hasResidentKey: true, hasUserVerification: true,
    isUserVerified: true, automaticPresenceSimulation: true } });
  await page.goto(BASE);
  await page.getByRole("button", { name: "Create account" }).click();
  await page.getByLabel("Username").fill(name);
  await page.getByLabel("Display name").fill(name);
  await page.getByRole("button", { name: "Create account with a passkey" }).click();
  await page.getByRole("heading", { name: "Send money" }).waitFor({ timeout: 10000 });
  return page;
}

async function pay(page, upi, amount) {
  await page.getByLabel("Pay to").selectOption("UPI");
  await page.getByLabel("UPI ID").fill(upi);
  await page.getByLabel("Amount (₹)").fill(amount);
  await page.getByRole("button", { name: "Check and send" }).click();
  await page.locator(".result h2").waitFor();
  return page.locator(".result h2").innerText();
}

// 1. Customer: two large first payments to new payees -> both held
const customer = await person("cust" + run);
const muleUpi = `ops.mule.${run}@okaxis`;
const okUpi = `ops.ok.${run}@okicici`;
ok((await pay(customer, muleUpi, String(MULE_AMT))) === "Extra check needed", `customer's ${inr(MULE_AMT)} to a new payee is held`);
ok((await pay(customer, okUpi, String(OK_AMT))) === "Extra check needed", "second large payment held too");

// 2. Analyst: the role comes from the operator, not from the analyst's own account
const analystName = "anal" + run;
const analyst = await person(analystName);
ok(!(await analyst.getByRole("button", { name: "Fraud ops" }).isVisible()), "no Fraud ops tab before the operator grants the role");
const grant = await fetch(`${API}/ops/analysts`, {
  method: "POST", headers: { "content-type": "application/json", "x-demo-operator-token": OPERATOR },
  body: JSON.stringify({ username: analystName }),
});
ok(grant.status === 200, "operator grants the analyst role");
await analyst.reload();
await analyst.getByRole("button", { name: "Fraud ops" }).click();
await analyst.getByTestId("ops").getByText(/ledger writes (on|off)/).waitFor();
const writes = (await analyst.getByTestId("ops").innerText()).includes("ledger writes on");

// 3. Queue and the passkey gate
const item = (text) => analyst.getByTestId("queue-item").filter({ hasText: text });
await item(inr(MULE_AMT)).waitFor();
await item(inr(MULE_AMT)).locator(".queue-head").click();
ok(await analyst.getByRole("button", { name: "Release" }).isDisabled(), "decisions are disabled until the analyst verifies");
await analyst.getByRole("button", { name: "Verify with passkey to decide" }).click();
await analyst.getByTestId("elevated").waitFor();
ok(true, "analyst verified with a passkey (Windows Hello in real use)");

// 4. Reject (and report, when ledger writes are on)
await analyst.getByLabel(/^Note/).fill("mule: same story as 2 earlier complaints");
if (writes) {
  await analyst.getByLabel("Severity").selectOption("HIGH");
  await analyst.getByRole("button", { name: "Reject and report to consortium" }).click();
  await analyst.getByText(`Reject and report to the consortium: ${inr(MULE_AMT)}`, { exact: false }).waitFor();
  ok(true, "rejected and reported to the consortium");
  // The filing goes through the outbox; the dashboard polls until the ledger has answered.
  const filing = analyst.getByTestId("filings").locator("tr").filter({ hasText: inr(MULE_AMT) }).first();
  await filing.getByTestId("filing-status").filter({ hasNotText: "Queued" }).waitFor({ timeout: 45000 });
  const st = await filing.getByTestId("filing-status").innerText();
  ok(/Reported|Confirmed|Already/.test(st), `ledger filing completed: ${st}`);
  ok(/[0-9a-f]{12}…/.test(await filing.innerText()), "filing shows the ledger report id");
} else {
  await analyst.getByRole("button", { name: "Reject", exact: true }).click();
  await analyst.getByText(`Reject: ${inr(MULE_AMT)}`, { exact: false }).waitFor();
  ok(true, "rejected (ledger writes are off on this server, so no report)");
}

// 5. Release the other one
await item(inr(OK_AMT)).locator(".queue-head").click();
await analyst.getByRole("button", { name: "Release" }).click();
await analyst.getByText(`Release: ${inr(OK_AMT)}`, { exact: false }).waitFor();
await item(inr(OK_AMT)).waitFor({ state: "detached", timeout: 5000 }).catch(() => {});
ok((await item(inr(OK_AMT)).count()) === 0, "released payment leaves the queue");

// 6. The customer sees both outcomes
await customer.reload();
await customer.getByRole("heading", { name: "Recent payments" }).waitFor();
await customer.getByTestId("reviewed").first().waitFor({ timeout: 5000 }).catch(() => {});
const rows = await customer.locator("table tbody tr").allInnerTexts();
if (process.env.DEBUG) console.log(rows);
ok(rows.some((r) => r.includes(inr(MULE_AMT)) && r.includes("Stopped after review")), "customer sees the rejected payment as stopped");
ok(rows.some((r) => r.includes(inr(OK_AMT)) && r.includes("Released after review")), "customer sees the released payment");

// 7. The analyst cannot review their own held payment
await analyst.getByRole("button", { name: "Send money" }).click();
await pay(analyst, `own.${run}@okaxis`, String(MULE_AMT + 1));
await analyst.getByRole("button", { name: "Fraud ops" }).click();
await analyst.getByTestId("ops").waitFor();
await analyst.waitForTimeout(500);
ok((await item(inr(MULE_AMT + 1)).count()) === 0,
  "an analyst's own held payment is not in their queue");

if (process.env.SCREENSHOT) await analyst.getByTestId("ops").screenshot({ path: process.env.SCREENSHOT });
await browser.close();
console.log(failures ? `\n${failures} FAILED` : "\nALL OPS BROWSER CHECKS PASSED");
process.exit(failures ? 1 : 0);
