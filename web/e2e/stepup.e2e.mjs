// Browser test for Module 3b: the customer's own step-up (confirm / scam answers / cancel) and the
// sandbox (a takeover session sees "Allowed" and a falling balance while nothing moves), then the
// analyst sees the flagged step-up first, the sandboxed session, and ends it.
//   node e2e/stepup.e2e.mjs
import { chromium } from "playwright";
const BASE = process.env.BASE_URL ?? "http://localhost:5173";
const API = process.env.API_URL ?? "http://localhost:8000";
const OPERATOR = process.env.OPERATOR_TOKEN ?? "demo-operator-token-change-me";
const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || undefined, args: ["--no-sandbox"] });
let failures = 0;
const ok = (cond, msg) => { console.log(cond ? "✔" : "✘", msg); if (!cond) failures++; };
const run = Date.now().toString(36);
const n = 100 + (Date.now() % 800);
const inr = (v) => "₹" + v.toLocaleString("en-US");
const money = (s) => Number(s.replace(/[^0-9.]/g, ""));

async function person(name) {
  const context = await browser.newContext({ viewport: { width: 1100, height: 1500 } });
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
  await page.getByLabel("Amount (₹)").fill(String(amount));
  await page.getByRole("button", { name: "Check and send" }).click();
  await page.locator(".result h2").waitFor();
  await page.waitForTimeout(300);
  return page.locator(".result h2").innerText();
}

async function answer(page, onCall, promised, knowsPayee) {
  const box = page.getByTestId("stepup");
  const pick = async (i, yes) => box.locator("fieldset").nth(i).getByLabel(yes ? "Yes" : "No").check();
  await pick(0, onCall);
  await pick(1, promised);
  await pick(2, knowsPayee);
}

const balance = async (page) => money(await page.getByTestId("balance").innerText());

// ---------------------------------------------------------------- step-up
const customer = await person("su" + run);
await customer.getByTestId("balance").waitFor();
const start = await balance(customer);

const A = 60000 + n;
ok((await pay(customer, `su.a.${run}@okaxis`, A)) === "Extra check needed", "large payment to a new payee is held");
ok(await customer.getByTestId("stepup").isVisible(), "the customer is offered their own check");
ok(await customer.getByRole("button", { name: "Confirm with passkey" }).isDisabled(), "confirm needs all three answers");
await answer(customer, false, false, true);
await customer.getByRole("button", { name: "Confirm with passkey" }).click();
await customer.getByTestId("stepup-result").waitFor();
ok((await customer.getByTestId("stepup-result").innerText()).includes("Confirmed. The payment has been sent."), "honest answers + passkey release it");
await customer.getByText("Confirmed by you").first().waitFor();
ok((await balance(customer)) === start - A, `balance drops by ${inr(A)} (money really sent)`);

const B = 61000 + n;
await pay(customer, `su.b.${run}@okaxis`, B);
await answer(customer, true, false, false);
await customer.getByRole("button", { name: "Confirm with passkey" }).click();
await customer.getByTestId("stepup-result").waitFor();
const warn = await customer.getByTestId("stepup-result").innerText();
ok(warn.includes("never ask you to move money") && warn.includes("fraud team"), "scam answer: warning, payment stays held");
const rowB = customer.locator("table tbody tr").filter({ hasText: inr(B) });
ok((await rowB.getByRole("button", { name: "Confirm or cancel" }).count()) === 0, "no second attempt offered");

const C = 62000 + n;
await pay(customer, `su.c.${run}@okaxis`, C);
await customer.getByRole("button", { name: "Cancel payment" }).click();
await customer.getByTestId("stepup-result").waitFor();
await customer.getByText("Cancelled by you").first().waitFor();
ok(true, "customer cancels a held payment");

// ---------------------------------------------------------------- sandbox
await customer.getByLabel("Operator token").fill(OPERATOR);
await customer.getByRole("button", { name: "Connect" }).click();
await customer.getByLabel("New device").waitFor();
await customer.getByLabel("SIM changed").selectOption("2");
await customer.getByLabel("New device").check();
await customer.waitForTimeout(400);
const beforeSandbox = await balance(customer);
const S1 = 45000 + n;
ok((await pay(customer, `sb.one.${run}@okaxis`, S1)) === "Allowed", "takeover session is shown 'Allowed'");
ok(await customer.getByTestId("sandbox-line").isVisible(), "analyst view (demo) says SANDBOX: nothing was sent");
await customer.getByRole("button", { name: "Reset signals" }).click();
await customer.waitForTimeout(400);
const S2 = 20000 + n;
ok((await pay(customer, `sb.two.${run}@okaxis`, S2)) === "Allowed", "signals off: the session stays in the sandbox");
ok((await customer.getByTestId("sandbox-line").innerText()).includes("session already sandboxed"), "...because the session is sandboxed");
await customer.waitForTimeout(300);
ok((await balance(customer)) === beforeSandbox - S1 - S2, "the sandboxed session sees a falling (fake) balance");

// ---------------------------------------------------------------- analyst
const analystName = "an" + run;
const analyst = await person(analystName);
await fetch(`${API}/ops/analysts`, { method: "POST", headers: { "content-type": "application/json", "x-demo-operator-token": OPERATOR },
  body: JSON.stringify({ username: analystName }) });
await analyst.reload();
await analyst.getByRole("button", { name: "Fraud ops" }).click();
await analyst.getByTestId("ops").getByText(/ledger writes (on|off)/).waitFor();
const first = analyst.getByTestId("queue-item").first();
ok((await first.innerText()).includes(inr(B)) && (await first.innerText()).includes("Customer described a scam"), "scam-flagged step-up is first in the queue");
await first.locator(".queue-head").click();
ok((await analyst.getByTestId("stepup-answers").innerText()).includes("on a call yes"), "analyst sees the customer's answers");
const sess = analyst.getByTestId("sandbox-session").filter({ hasText: inr(S1 + S2) });
await sess.waitFor();
ok((await sess.innerText()).includes("2 attempt(s)"), "sandboxed session listed with its attempts");
if ((await analyst.getByTestId("ops").innerText()).includes("ledger writes on")) {
  await analyst.getByTestId("filings").getByText("sandbox capture").first().waitFor({ timeout: 30000 });
  ok(true, "sandbox captures are filed to the ledger");
}
await analyst.getByRole("button", { name: "Verify with passkey to decide" }).click();
await analyst.getByTestId("elevated").waitFor();
await sess.getByRole("button", { name: "End session" }).click();
await sess.getByText("ended").waitFor();
await customer.reload();
await customer.getByRole("button", { name: "Sign in with a passkey" }).waitFor({ timeout: 8000 }).catch(() => {});
ok(await customer.getByRole("button", { name: "Sign in with a passkey" }).isVisible(), "ending the session signs the attacker out");

if (process.env.SCREENSHOT) await analyst.getByTestId("ops").screenshot({ path: process.env.SCREENSHOT });
await browser.close();
console.log(failures ? `\n${failures} FAILED` : "\nALL STEP-UP / SANDBOX BROWSER CHECKS PASSED");
process.exit(failures ? 1 : 0);
