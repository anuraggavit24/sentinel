// Browser test for the Ledger page: an analyst's report becomes a real block on the chain; the page shows
// it linked by hash, who endorsed it, the 5-step Fabric flow, and the payee check walks the payment path.
// Needs the stack with ledger writes on (live Fablo, or the dev peer).
//   node e2e/ledger-ui.e2e.mjs
import { chromium } from "playwright";
const BASE = process.env.BASE_URL ?? "http://localhost:5173";
const API = process.env.API_URL ?? "http://localhost:8000";
const OPERATOR = process.env.OPERATOR_TOKEN ?? "demo-operator-token-change-me";
const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || undefined, args: ["--no-sandbox"] });
let failures = 0;
const ok = (cond, msg) => { console.log(cond ? "✔" : "✘", msg); if (!cond) failures++; };
const run = Date.now().toString(36);
const amount = 63000 + (Date.now() % 800);
const inr = "₹" + amount.toLocaleString("en-US");
const upi = `ledger.ui.${run}@okaxis`;

async function person(name) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 1600 } });
  const page = await context.newPage();
  const cdp = await context.newCDPSession(page);
  await cdp.send("WebAuthn.enable");
  await cdp.send("WebAuthn.addVirtualAuthenticator", { options: {
    protocol: "ctap2", transport: "internal", hasResidentKey: true, hasUserVerification: true,
    isUserVerified: true, automaticPresenceSimulation: true } });
  await page.goto(BASE);
  if (process.env.SHOTS && name.startsWith("lc")) await page.screenshot({ path: process.env.SHOTS + "/landing.png", fullPage: true });
  await page.getByRole("button", { name: "Create account" }).click();
  await page.getByLabel("Username").fill(name);
  await page.getByLabel("Display name").fill(name);
  await page.getByRole("button", { name: "Create account with a passkey" }).click();
  await page.getByRole("heading", { name: "Send money" }).waitFor({ timeout: 10000 });
  return page;
}

// Customer: a held payment
const customer = await person("lc" + run);
await customer.getByLabel("Pay to").selectOption("UPI");
await customer.getByLabel("UPI ID").fill(upi);
await customer.getByLabel("Amount (₹)").fill(String(amount));
await customer.getByRole("button", { name: "Check and send" }).click();
await customer.locator(".result h2").waitFor();

// Analyst: reject and report -> the outbox files it -> a block
const name = "la" + run;
const analyst = await person(name);
await fetch(`${API}/ops/analysts`, { method: "POST", headers: { "content-type": "application/json", "x-demo-operator-token": OPERATOR }, body: JSON.stringify({ username: name }) });
await analyst.reload();
await analyst.getByRole("button", { name: "Fraud ops" }).click();
await analyst.getByRole("button", { name: "Verify with passkey to decide" }).click();
await analyst.getByTestId("elevated").waitFor();
const item = analyst.getByTestId("queue-item").filter({ hasText: inr });
await item.locator(".queue-head").click();
await item.getByRole("button", { name: "Reject and report to consortium" }).click();
const filing = analyst.getByTestId("filings").locator("tr").filter({ hasText: inr }).first();
await filing.getByRole("button", { name: "See it on the ledger" }).waitFor({ timeout: 45000 });
ok(true, "report filed: the filing has a ledger transaction id");

// The Ledger page opens on that transaction
await filing.getByRole("button", { name: "See it on the ledger" }).click();
await analyst.getByTestId("ledger").waitFor();
const pipe = analyst.getByTestId("pipeline");
await pipe.waitFor();
await analyst.waitForTimeout(2600);
ok((await pipe.locator("li.on").count()) === 5, "the 5 Fabric steps light up: propose, endorse, order, validate, commit");
const pt = await pipe.innerText();
ok(/BANK A \+ BANK [BC]|Bank A \+ Bank [BC]/i.test(pt), "two banks endorsed it");
ok(/block #\d+/i.test(pt), "and it names the block it landed in");
const trace = analyst.getByTestId("trace");
ok((await trace.innerText()).toLowerCase().includes("reportmule"), "the traced transaction is the reportMule");
ok((await trace.locator(".endorser").count()) >= 2, "endorser badges shown");
const height = Number(await analyst.getByTestId("height").locator("b").innerText());
ok(height >= 2, `chain height shown (${height})`);
const links = await analyst.getByTestId("chain").locator(".link-arrow").allInnerTexts();
ok(links.length >= 1 && links.every((t) => t.includes("linked")), `blocks linked by hash (${links.length} links checked)`);
ok((await analyst.getByTestId("chain").innerText()).includes("data hash matches"), "data hashes recomputed and matching");
ok(await analyst.getByTestId("network").isVisible(), "network diagram shown");

// Payee check walks the payment path
const check = analyst.getByTestId("payee-check");
await check.getByLabel("Payee UPI ID").fill(upi);
await check.getByRole("button", { name: "Check" }).click();
await check.getByTestId("lookup-steps").waitFor();
const steps = await check.getByTestId("lookup-steps").innerText();
ok(/KEYED HASH|Keyed hash/i.test(steps) && /\+10 points|\+50 points/.test(steps), "payee check: keyed hash, ledger answer, points");
ok(!steps.includes(upi), "the full payee id is never shown back");

// Customer sees the chain but not the arguments
await customer.getByRole("button", { name: "Ledger" }).click();
await customer.getByTestId("chain").waitFor();
await customer.getByTestId("block").last().click();
const detail = await customer.getByTestId("block-detail").innerText();
ok(detail.includes("analysts only"), "customers see blocks, not transaction arguments");
ok((await customer.getByTestId("payee-check").count()) === 0, "customers get no payee check");

if (process.env.SHOTS) {
  await analyst.getByTestId("ledger").screenshot({ path: process.env.SHOTS + "/ledger.png" });
  await analyst.getByRole("button", { name: "Send money" }).click();
  await analyst.screenshot({ path: process.env.SHOTS + "/send.png", fullPage: true });
}
await browser.close();
console.log(failures ? `\n${failures} FAILED` : "\nALL LEDGER PAGE BROWSER CHECKS PASSED");
process.exit(failures ? 1 : 0);
