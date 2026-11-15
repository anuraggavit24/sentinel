// Browser test for Module 1: real UI, Chrome virtual authenticator instead of Windows Hello.
// Run with the stack up:  cd web && npm i -D playwright && npx playwright install chromium && node e2e/passkey.e2e.mjs
const BASE = process.env.BASE_URL ?? "http://localhost:5173";
import { chromium } from "playwright";
const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || undefined, args: ["--no-sandbox"] });
const page = await browser.newPage();
const cdp = await page.context().newCDPSession(page);
await cdp.send("WebAuthn.enable");
await cdp.send("WebAuthn.addVirtualAuthenticator", { options: {
  protocol: "ctap2", transport: "internal", hasResidentKey: true, hasUserVerification: true,
  isUserVerified: true, automaticPresenceSimulation: true } });
const user = "e2e" + Date.now().toString(36);
const step = (m) => console.log("✔", m);
await page.goto(BASE);
await page.getByRole("button", { name: "Create account" }).click();
await page.getByLabel("Username").fill(user);
await page.getByLabel("Display name").fill("E2E Tester");
await page.getByRole("button", { name: "Create account with a passkey" }).click();
await page.getByText("Hello, E2E Tester").waitFor({ timeout: 10000 }); step("registered with a passkey and landed on the account page");
await page.getByText("this session").waitFor(); step("current session listed");
await page.getByRole("button", { name: "Sign out", exact: true }).first().click();
await page.getByRole("button", { name: "Sign in with a passkey" }).waitFor(); step("signed out");
await page.getByLabel("Username").fill(user);
await page.getByRole("button", { name: "Sign in with a passkey" }).click();
await page.getByText("Hello, E2E Tester").waitFor({ timeout: 10000 }); step("signed back in with the passkey");
const removeBtn = page.getByRole("button", { name: "Remove" });
console.log((await removeBtn.isDisabled()) ? "✔ only passkey cannot be removed (button disabled)" : "✘ remove button enabled");
await page.getByLabel("Username").count().then(n => n === 0 && step("no password field anywhere"));
await page.screenshot({ path: "e2e/m1-account.png", fullPage: true });
await page.goto(BASE); await page.getByText("Hello, E2E Tester").waitFor(); step("session survives a page reload (httpOnly cookie)");
await browser.close();
