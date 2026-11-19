// Browser test for Module 4: an analyst asks the copilot about a held payment and gets a cited answer.
// Works in both modes (extractive without ANTHROPIC_API_KEY, model-written with it).
//   node e2e/copilot.e2e.mjs
import { chromium } from "playwright";
const BASE = process.env.BASE_URL ?? "http://localhost:5173";
const API = process.env.API_URL ?? "http://localhost:8000";
const OPERATOR = process.env.OPERATOR_TOKEN ?? "demo-operator-token-change-me";
const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || undefined, args: ["--no-sandbox"] });
let failures = 0;
const ok = (cond, msg) => { console.log(cond ? "✔" : "✘", msg); if (!cond) failures++; };
const run = Date.now().toString(36);
const amount = 47000 + (Date.now() % 800);
const inr = "₹" + amount.toLocaleString("en-US");

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

// A coached customer: active call during a large first payment
const customer = await person("cp" + run);
await customer.getByLabel("Operator token").fill(OPERATOR);
await customer.getByRole("button", { name: "Connect" }).click();
await customer.getByLabel("Phone call active").check();
await customer.waitForTimeout(400);
await customer.getByLabel("Pay to").selectOption("UPI");
await customer.getByLabel("UPI ID").fill(`safe.account.${run}@okaxis`);
await customer.getByLabel("Amount (₹)").fill(String(amount));
await customer.getByRole("button", { name: "Check and send" }).click();
await customer.locator(".result h2").waitFor();
ok((await customer.locator(".result h2").innerText()) === "Extra check needed", "coached payment is held");

const analystName = "cpa" + run;
const analyst = await person(analystName);
await fetch(`${API}/ops/analysts`, { method: "POST", headers: { "content-type": "application/json", "x-demo-operator-token": OPERATOR },
  body: JSON.stringify({ username: analystName }) });
await analyst.reload();
await analyst.getByRole("button", { name: "Fraud ops" }).click();
const item = analyst.getByTestId("queue-item").filter({ hasText: inr });
await item.waitFor();
await item.locator(".queue-head").click();
await item.getByText("Ask the copilot about this case").click();
await item.getByRole("button", { name: "What does policy say I should do?" }).click();
const ans = item.getByTestId("copilot-answer");
await ans.waitFor({ timeout: 40000 });
const text = await ans.innerText();
ok(/Extractive mode|language model/.test(text), `answer says how it was made (${text.includes("Extractive") ? "extractive" : "model"})`);
ok((await ans.locator("button.cite").count()) >= 2, "answer carries citations");
ok(text.includes("This case") && text.includes("Consortium ledger"), "sources include the case and the ledger");
ok(/digital arrest|call/i.test(text), "the case's active call steers the policy it retrieves");
await ans.locator("button.cite").first().click();
ok((await ans.getByTestId("source-text").first().innerText()).length > 20, "a citation opens its source text");
ok(!text.includes("cp" + run + "@") && !text.includes(`safe.account.${run}`), "no raw payee identifier in the answer or sources");

await item.getByLabel("Ask the copilot").fill("Has this payee been reported to the consortium?");
await item.getByRole("button", { name: "Ask", exact: true }).click();
await analyst.waitForTimeout(800);
await ans.waitFor();
ok((await ans.innerText()).includes("Consortium ledger"), "a typed question works too");

if (process.env.SCREENSHOT) await item.screenshot({ path: process.env.SCREENSHOT });
await browser.close();
console.log(failures ? `\n${failures} FAILED` : "\nALL COPILOT BROWSER CHECKS PASSED");
process.exit(failures ? 1 : 0);
