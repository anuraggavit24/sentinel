// Browser test for slice 2b: real keyboard events with human-like timing build a behaviour profile,
// then a different typing rhythm is flagged. Stack up with DEMO_SIGNALS=1 SHOW_ANALYST_VIEW=1.
import { chromium } from "playwright";
const BASE = process.env.BASE_URL ?? "http://localhost:5173";
const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || undefined, args: ["--no-sandbox"] });
const page = await browser.newPage();
const cdp = await page.context().newCDPSession(page);
await cdp.send("WebAuthn.enable");
await cdp.send("WebAuthn.addVirtualAuthenticator", { options: {
  protocol: "ctap2", transport: "internal", hasResidentKey: true, hasUserVerification: true,
  isUserVerified: true, automaticPresenceSimulation: true } });
let failures = 0;
const ok = (cond, msg) => { console.log(cond ? "✔" : "✘", msg); if (!cond) failures++; };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let seed = 42;
const rand = () => ((seed = (seed * 16807) % 2147483647) / 2147483647);
const jitter = (ms, spread) => Math.max(5, ms + (rand() - 0.5) * 2 * spread);

// Type like a person: hold each key for `dwell` ms, pause `flight` ms between keys.
async function typeHuman(locator, text, dwell, flight) {
  await locator.click();
  await locator.fill("");
  for (const ch of text) {
    await page.keyboard.down(ch);
    await sleep(jitter(dwell, dwell * 0.12));
    await page.keyboard.up(ch);
    await sleep(jitter(flight, flight * 0.12));
  }
}

async function payTyped(upi, amount, dwell, flight) {
  await page.getByLabel("Pay to").selectOption("UPI");
  await typeHuman(page.getByLabel("UPI ID"), upi, dwell, flight);
  await typeHuman(page.getByLabel("Amount (₹)"), amount, dwell, flight);
  await page.mouse.move(200, 300); await page.mouse.move(400, 520, { steps: 12 });
  await page.getByRole("button", { name: "Check and send" }).click();
  await page.getByTestId("behaviour-status").waitFor();
  await sleep(250);
  return {
    status: await page.getByTestId("behaviour-status").innerText(),
    analyst: await page.locator(".analyst .reasons li").allInnerTexts(),
    customer: await page.locator(".reasons.plain li").allInnerTexts(),
  };
}

await page.goto(BASE);
await page.getByRole("button", { name: "Create account" }).click();
await page.getByLabel("Username").fill("typist" + Date.now().toString(36));
await page.getByLabel("Display name").fill("Typist");
await page.getByRole("button", { name: "Create account with a passkey" }).click();
await page.getByRole("heading", { name: "Send money" }).waitFor({ timeout: 10000 });

// Guided enrolment: passkey check, then 5 practice samples typed by hand
ok((await page.getByTestId("behaviour-profile").innerText()).includes("Not set up yet"), "profile starts not set up");
await page.getByRole("button", { name: "Set up typing profile" }).click();
for (let i = 0; i < 5; i++) {
  await page.getByText(`Sample ${i + 1} of 5`).waitFor({ timeout: 10000 });
  const codes = await page.locator(".practice code").allInnerTexts();
  await typeHuman(page.getByLabel("Practice UPI ID"), codes[0], 95, 140);
  await typeHuman(page.getByLabel("Practice amount"), codes[1], 95, 140);
  await page.getByRole("button", { name: "Save sample" }).click();
}
await page.getByText("Active: built from 5 clean samples").waitFor({ timeout: 10000 });
ok(true, "after a passkey check and 5 typed samples the profile is active");

let r = await payTyped("corner.shop@okicici", "300", 95, 140);
ok(r.status.includes("robust-z") && !r.analyst.some((t) => t.includes("Typing and mouse use differ")), `owner's rhythm not flagged: ${r.status}`);
const thisPayment = await page.getByTestId("behaviour-this-payment").innerText();
ok(thisPayment.includes("typing captured") && thisPayment.includes("added to your profile"), `diagnostics: ${thisPayment}`);
ok(!(await page.locator(".analyst").innerText()).includes("set by the demo operator"), "no false 'operator signals' note");

r = await payTyped("corner.shop@okicici", "300", 35, 45);
ok(r.analyst.some((t) => t.includes("Typing and mouse use differ")), `a different rhythm is flagged: ${r.status}`);
ok(r.customer.some((t) => t.includes("unusual for you")), "customer sees a plain-language note, no numbers");

// Pasting into the payment form: no typing data, profile exists -> not a free pass
await page.getByLabel("Pay to").selectOption("UPI");
await page.getByLabel("UPI ID").fill("pasted.payee@okicici");
await page.getByLabel("Amount (₹)").fill("300");
await page.getByRole("button", { name: "Check and send" }).click();
await page.getByTestId("behaviour-this-payment").waitFor();
await sleep(300);
const pasted = await page.locator(".analyst .reasons li").allInnerTexts();
ok(pasted.some((t) => t.includes("No usable typing data")), "filled without typing -> BEHAVIOUR_MISSING");

await browser.close();
process.exit(failures ? 1 : 0);
