// Browser test for slice 2c: a payment to a payee CONFIRMED on the consortium ledger is held, the
// customer sees one plain sentence, and the analyst view shows the ledger report.
// Live run: ./scripts/ledger_smoke.sh prints a confirmed UPI ID, then
//   LEDGER_UPI=mule.<run>@okaxis node e2e/ledger.e2e.mjs
import { chromium } from "playwright";
const BASE = process.env.BASE_URL ?? "http://localhost:5173";
const MULE = process.env.LEDGER_UPI;
if (!MULE) { console.log("Set LEDGER_UPI to a UPI ID confirmed on the ledger (scripts/ledger_smoke.sh prints one)"); process.exit(2); }
const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || undefined, args: ["--no-sandbox"] });
const page = await browser.newPage({ viewport: { width: 1100, height: 1400 } });
const cdp = await page.context().newCDPSession(page);
await cdp.send("WebAuthn.enable");
await cdp.send("WebAuthn.addVirtualAuthenticator", { options: {
  protocol: "ctap2", transport: "internal", hasResidentKey: true, hasUserVerification: true,
  isUserVerified: true, automaticPresenceSimulation: true } });
let failures = 0;
const ok = (cond, msg) => { console.log(cond ? "✔" : "✘", msg); if (!cond) failures++; };

await page.goto(BASE);
await page.getByRole("button", { name: "Create account" }).click();
await page.getByLabel("Username").fill("ledger" + Date.now().toString(36));
await page.getByLabel("Display name").fill("Ledger Tester");
await page.getByRole("button", { name: "Create account with a passkey" }).click();
await page.getByRole("heading", { name: "Send money" }).waitFor({ timeout: 10000 });

async function send(upi, amount) {
  await page.getByLabel("Pay to").selectOption("UPI");
  await page.getByLabel("UPI ID").fill(upi);
  await page.getByLabel("Amount (₹)").fill(amount);
  await page.getByRole("button", { name: "Check and send" }).click();
  await page.getByTestId("consortium-status").waitFor();
  await page.waitForTimeout(300);
  return {
    band: await page.locator(".result h2").innerText(),
    customer: await page.locator(".reasons.plain li").allInnerTexts(),
    ledger: await page.getByTestId("consortium-status").innerText(),
    analyst: await page.locator(".analyst .reasons li").allInnerTexts(),
  };
}

if (process.env.SKIP_CLEAR !== "1") {
  const r = await send(`clear.${Date.now()}@okicici`, "120");
  ok(r.ledger.includes("no reports"), `fresh payee is clear on the ledger (${r.ledger.split("\n")[0]})`);
  ok(r.band === "Allowed", "and the small payment is allowed");
}

const r = await send(MULE, "120");
ok(r.band === "Extra check needed", `₹120 to a confirmed mule is held (${r.band})`);
ok(r.customer.includes("Banks have received fraud reports about this payee"), "customer sees one plain sentence");
ok(!r.customer.join(" ").match(/CONFIRMED|weight|Bank[ABC]/), "customer sees no ledger details");
ok(r.ledger.includes("CONFIRMED") && /report\(s\)/.test(r.ledger), "analyst view shows the confirmed report");
ok(r.analyst.some((t) => t.includes("+50") && t.includes("CONFIRMED mule")), "analyst sees +50 for the ledger");

if (process.env.SCREENSHOT) await page.locator(".result").screenshot({ path: process.env.SCREENSHOT });
await browser.close();
console.log(failures ? `\n${failures} FAILED` : "\nALL LEDGER BROWSER CHECKS PASSED");
process.exit(failures ? 1 : 0);
