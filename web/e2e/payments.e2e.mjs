// Browser test for Module 2a: send money, read the decision and its reasons.
// Run with the stack up (DEMO_SIGNALS=1, SHOW_ANALYST_VIEW=1):
//   OPERATOR_TOKEN=demo-operator-token-change-me node e2e/payments.e2e.mjs
import { chromium } from "playwright";
const BASE = process.env.BASE_URL ?? "http://localhost:5173";
const OPERATOR_TOKEN = process.env.OPERATOR_TOKEN ?? "demo-operator-token-change-me";
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
await page.getByLabel("Username").fill("pay" + Date.now().toString(36));
await page.getByLabel("Display name").fill("Payment Tester");
await page.getByRole("button", { name: "Create account with a passkey" }).click();
await page.getByRole("heading", { name: "Send money" }).waitFor({ timeout: 10000 });
ok(true, "signed up and landed on Send money");

async function send(fill, amount) {
  await fill();
  await page.getByLabel("Amount (₹)").fill(amount);
  await page.getByRole("button", { name: "Check and send" }).click();
  await page.getByTestId("score").waitFor();
  await page.waitForTimeout(300);
  return { band: await page.locator(".result h2").innerText(), score: Number((await page.getByTestId("score").innerText()).split("/")[0]), reasons: await page.locator(".reasons li").allInnerTexts() };
}

// Operator console needs its token
await page.getByLabel("Operator token").fill(OPERATOR_TOKEN);
await page.getByRole("button", { name: "Connect" }).click();
await page.getByLabel("New device").waitFor();
ok(true, "operator console connected with its token");

// 1. Ordinary first payment
let r = await send(async () => { await page.getByLabel("Pay to").selectOption("UPI"); await page.getByLabel("UPI ID").fill("chaiwala@okicici"); }, "120");
ok(r.band === "Allowed" && r.score === 15 && r.reasons.some((t) => t.includes("First payment")), `small first payment allowed (score ${r.score})`);
ok(!(await page.locator(".reasons.plain").innerText()).includes("+"), "customer reasons show no points");

// 2. Account takeover: SIM swapped 2 h ago + new device, large payment to a new account
await page.getByLabel("SIM changed").selectOption("2");
await page.getByLabel("New device").check();
await page.waitForTimeout(400);
r = await send(async () => { await page.getByLabel("Pay to").selectOption("ACCOUNT"); await page.getByLabel("IFSC").fill("utib0000789"); await page.getByLabel("Account number").fill("917020001234"); }, "45000");
ok(r.band.startsWith("Held") && r.score >= 70, `takeover signals -> sandbox (score ${r.score})`);
ok(r.reasons.some((t) => t.includes("SIM was changed")) && r.reasons.some((t) => t.includes("device not seen")), "takeover reasons explained");

// 3. Coached customer: on a call with screen sharing, large payment to a new UPI
await page.getByRole("button", { name: "Reset signals" }).click();
await page.getByLabel("Phone call active").check();
await page.getByLabel("Screen being shared").check();
await page.waitForTimeout(400);
r = await send(async () => { await page.getByLabel("Pay to").selectOption("UPI"); await page.getByLabel("UPI ID").fill("refund.desk@okaxis"); }, "90000");
ok(r.band === "Extra check needed", `coached customer -> step-up, not sandbox (score ${r.score})`);
ok(r.reasons.some((t) => t.includes("phone call")), "coaching reasons explained");

// 3b. The "send ₹1 first to verify" trick on the same call
await page.getByLabel("Pay to").selectOption("UPI");
r = await send(async () => { await page.getByLabel("UPI ID").fill("kyc.verify@okaxis"); }, "1");
r = await send(async () => { await page.getByLabel("UPI ID").fill("kyc.verify@okaxis"); }, "90000");
ok(r.band === "Extra check needed", `₹1 test payment does not unlock ₹90,000 on a call (score ${r.score})`);

// 4. FRI mock
await page.getByRole("button", { name: "Reset signals" }).click();
await page.waitForTimeout(300);
r = await send(async () => { await page.getByLabel("Pay to").selectOption("PHONE"); await page.getByLabel("Mobile number").fill("99999 00003"); }, "500");
ok(r.reasons.some((t) => t.includes("FRI")), `FRI very-high payee flagged in the analyst view (score ${r.score})`);
const customerText = await page.locator(".reasons.plain").innerText();
ok(customerText.includes("telecom fraud-risk") && !customerText.includes("FRI") && !/very high/i.test(customerText), "customer sees a generic telecom warning, not the FRI category");

// 4b. Double-click on submit sends one payment
const before = await page.locator("tbody tr").count();
await page.getByLabel("Pay to").selectOption("UPI");
await page.getByLabel("UPI ID").fill("double.click@okicici");
await page.getByLabel("Amount (₹)").fill("250");
await page.getByRole("button", { name: "Check and send" }).dblclick();
await page.waitForTimeout(1500);
ok((await page.locator("tbody tr").count()) === before + 1, "double-click created exactly one payment");

// 5. Validation
await page.getByLabel("Pay to").selectOption("ACCOUNT");
await page.getByLabel("IFSC").fill("BAD");
await page.getByLabel("Account number").fill("123456789");
await page.getByLabel("Amount (₹)").fill("100");
await page.getByRole("button", { name: "Check and send" }).click();
await page.locator(".error").waitFor();
ok((await page.locator(".error").innerText()).includes("IFSC"), "invalid IFSC explained");

ok((await page.locator("tbody tr").count()) === 7, "7 payments in history");
await page.getByRole("button", { name: "Passkeys and sessions" }).click();
ok(!(await page.getByText("last used never").count()), "passkey shows a last-used time");
await page.getByRole("button", { name: "Send money" }).click();
await page.screenshot({ path: "e2e/m2a-send-money.png", fullPage: true });
await browser.close();
process.exit(failures ? 1 : 0);
