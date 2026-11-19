// Browser test for #/demo, the three-bank network: report at Bank A, confirm at Bank B, blocked at Bank C;
// per-bank ledger views (private details vs hash only); verified signatures; the tamper check; keys.
// Needs the stack with DEMO_LOGIN=1 and ledger writes on (live Fablo after `fablo recreate`, or the dev peers).
//   node e2e/demo.e2e.mjs          (SHOTS=/some/dir to save screenshots)
import { chromium } from "playwright";
const BASE = process.env.BASE_URL ?? "http://localhost:5173";
const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || undefined, args: ["--no-sandbox"] });
let failures = 0;
const ok = (cond, msg) => { console.log(cond ? "✔" : "✘", msg); if (!cond) failures++; };
const shot = async (page, name) => { if (process.env.SHOTS) await page.screenshot({ path: `${process.env.SHOTS}/${name}.png`, fullPage: true }); };

const context = await browser.newContext({ viewport: { width: 1440, height: 1300 } });
const page = await context.newPage();
page.on("dialog", (d) => d.accept());

async function loginAs(name) {
  if (await page.getByRole("button", { name: "Switch customer" }).isVisible().catch(() => false)) {
    await page.getByRole("button", { name: "Switch customer" }).click();
  }
  await page.getByRole("button", { name: new RegExp(name) }).click();
  await page.getByTestId("customer-head").getByRole("heading", { name }).waitFor({ timeout: 10000 });
}
const balance = async () => (await page.getByTestId("balance").innerText()).trim();
async function sendToScammer(amount) {
  await page.getByRole("button", { name: /^scammer/ }).click();
  await page.getByLabel("Amount").fill(String(amount));
  await page.getByRole("button", { name: "Send", exact: true }).click();
}
const firstTransfer = () => page.getByTestId("transfer").first();
async function waitOnLedger() {
  await firstTransfer().getByText(/on the ledger · block #\d+/).waitFor({ timeout: 20000 });
}
async function viewAs(bank) {
  await page.getByTestId(`view-${bank}`).click();
  await page.getByRole("heading", { name: new RegExp(`as ${bank.replace("MSP", "").replace("Bank", "Bank ")} sees it`, "i") }).waitFor();
}

await page.goto(`${BASE}/#/demo`);
await page.getByRole("heading", { name: /Three banks/i }).waitFor({ timeout: 10000 });
ok(await page.getByTestId("bank-BankAMSP").isVisible() && await page.getByTestId("bank-BankCMSP").isVisible(), "login page shows the three banks");
await shot(page, "demo-login");

// Fresh round (new scammer account), as Asha.
await loginAs("Asha Rao");
await page.getByRole("button", { name: "Reset the story" }).click();
await page.waitForTimeout(800);
ok((await balance()).includes("1,00,000"), "Asha starts with ₹1,00,000");

// 1. Asha (Bank A) pays the scammer: allowed, recorded on the ledger, then reported.
await sendToScammer(20000);
await page.getByTestId("last-result").getByRole("heading", { name: "Sent" }).waitFor({ timeout: 10000 });
ok((await balance()).includes("80,000"), "money left Asha's account");
await waitOnLedger();
ok(true, "the payment was recorded on the ledger (block number shown)");
await firstTransfer().getByRole("button", { name: /I was scammed/ }).click();
await firstTransfer().getByTestId("report-line").waitFor({ timeout: 20000 });
ok((await firstTransfer().getByTestId("report-line").innerText()).includes("PENDING"), "Bank A filed a report: PENDING");

// Bank A can read its own payment; Bank B sees only the hash.
await page.getByRole("button", { name: "Payments", exact: true }).click();
await page.getByTestId("ledger-payment").first().waitFor({ timeout: 10000 });
ok(await page.getByTestId("payment-details").first().innerText().then((t) => t.includes("20,000")), "as Bank A: the payment details are visible");
ok(await page.getByTestId("bank-copy").first().innerText().then((t) => t.includes("matches the chain")), "Bank A's database copy matches the hash on the chain");
await shot(page, "demo-asha");
await viewAs("BankBMSP");
await page.getByTestId("payment-hidden").first().waitFor({ timeout: 10000 });
const hidden = await page.getByTestId("ledger-payment").first().innerText();
ok(hidden.includes("PRIVATE") && !hidden.includes("20,000") && !hidden.includes("asha@banka"), "as Bank B: only the hash, no amount, no UPI IDs");
await shot(page, "demo-private");
await viewAs("RegulatorMSP");
ok(await page.getByTestId("payment-hidden").first().isVisible(), "as the Regulator: hash only too");

// 2. Bilal (Bank B): warned, sends anyway, reports -> CONFIRMED.
await loginAs("Bilal Khan");
await sendToScammer(15000);
await page.getByTestId("warning").waitFor({ timeout: 10000 });
ok((await page.getByTestId("warning").innerText()).includes("reported by"), "Bank B warns: another bank reported this payee");
await page.getByRole("button", { name: "Send anyway" }).click();
await page.getByTestId("last-result").getByRole("heading", { name: "Sent after a warning" }).waitFor({ timeout: 10000 });
await waitOnLedger();
await firstTransfer().getByRole("button", { name: /I was scammed/ }).click();
await firstTransfer().getByTestId("report-line").waitFor({ timeout: 20000 });
const confirmLine = await firstTransfer().getByTestId("report-line").innerText();
ok(confirmLine.includes("Bank B confirmed") && confirmLine.includes("CONFIRMED"), "Bank B confirmed Bank A's report: CONFIRMED");
await shot(page, "demo-bilal");

// 3. Chitra (Bank C): blocked, no money moves.
await loginAs("Chitra Nair");
await sendToScammer(5000);
await page.getByTestId("last-result").getByRole("heading", { name: "Payment blocked" }).waitFor({ timeout: 10000 });
ok((await balance()).includes("1,00,000"), "blocked: Chitra's balance is unchanged");
await shot(page, "demo-blocked");

// Blocks and signatures.
await page.getByRole("button", { name: "Blocks and signatures" }).click();
await page.getByTestId("mini-block").first().waitFor({ timeout: 10000 });
const sigs = await page.getByTestId("sig").allInnerTexts();
ok(sigs.length >= 3 && sigs.every((s) => s.includes("signature valid")), `every signature in the newest block checks out (${sigs.length})`);
ok(sigs.some((s) => s.includes("issued by")), "certificates checked against each bank's CA");
// Find a payment block: it names its private collection.
const blocks = page.getByTestId("mini-block");
let sawPrivate = false;
for (let i = 0; i < (await blocks.count()) && !sawPrivate; i++) {
  if ((await blocks.nth(i).innerText()).includes("recordPayment")) {
    await blocks.nth(i).click();
    sawPrivate = (await page.getByTestId("private-note").first().innerText()).includes("pay_Bank");
  }
}
ok(sawPrivate, "a payment block shows its private collection and only a hash");
const links = await page.locator(".mini-link.broken").count();
ok(links === 0, "every block links to the one before (no broken arrows)");
await shot(page, "demo-blocks");

// Keys.
await page.getByRole("button", { name: "Keys", exact: true }).click();
await page.getByTestId("keys").waitFor({ timeout: 10000 });
const keyText = await page.getByTestId("keys").innerText();
ok(keyText.includes("EC P-256 public key") && keyText.includes("signed by the bank's CA") && !keyText.includes("PRIVATE KEY"),
  "keys tab: public keys and CA checks, never a private key");
await shot(page, "demo-keys");

// Tamper with Asha's database copy: the chain catches it; undo restores the match.
await loginAs("Asha Rao");
await page.getByRole("button", { name: /Tamper/ }).first().click();
await page.getByRole("button", { name: "Payments", exact: true }).click();
await page.getByTestId("bank-copy").filter({ hasText: "does NOT match" }).first().waitFor({ timeout: 10000 });
ok(true, "after editing the bank's database copy, its hash no longer matches the chain");
await shot(page, "demo-tamper");
await page.getByRole("button", { name: "Undo the database edit" }).first().click();
await page.getByTestId("bank-copy").filter({ hasText: "matches the chain" }).first().waitFor({ timeout: 10000 });
ok(true, "undoing the edit restores the match");

await browser.close();
if (failures) { console.error(`${failures} DEMO CHECK(S) FAILED`); process.exit(1); }
console.log("ALL DEMO BROWSER CHECKS PASSED");
