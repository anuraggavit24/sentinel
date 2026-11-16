// Browser test for slice 2b part 2: a volunteer joins the typing study, types a full session with real
// keyboard events, cannot start session 2 early, resumes with their code, and withdraws.
// Stack up with STUDY_ENABLED=1. Needs the operator token to issue invites.
import { chromium } from "playwright";
const BASE = process.env.BASE_URL ?? "http://localhost:5173";
const API = process.env.API_URL ?? "http://localhost:8000";
const OPERATOR = process.env.DEMO_OPERATOR_TOKEN ?? "demo-operator-token-change-me";
const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || undefined, args: ["--no-sandbox"] });
const page = await browser.newPage();
page.on("dialog", (d) => d.accept());
let failures = 0;
const ok = (cond, msg) => { console.log(cond ? "✔" : "✘", msg); if (!cond) failures++; };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let seed = 7;
const rand = () => ((seed = (seed * 16807) % 2147483647) / 2147483647);
const jitter = (ms, spread) => Math.max(5, ms + (rand() - 0.5) * 2 * spread);

async function typeHuman(locator, text, dwell = 95, flight = 150) {
  await locator.click();
  await locator.fill("");
  for (const ch of text) {
    await page.keyboard.down(ch);
    await sleep(jitter(dwell, dwell * 0.15));
    await page.keyboard.up(ch);
    await sleep(jitter(flight, flight * 0.15));
  }
}

async function newInvite() {
  const r = await fetch(`${API}/research/invites`, {
    method: "POST", headers: { "content-type": "application/json", "x-demo-operator-token": OPERATOR },
    body: JSON.stringify({ count: 1 }),
  });
  return (await r.json()).codes[0];
}

async function typePrompt(override) {
  const [upi, amount] = await page.getByTestId("study-progress").locator("code").allInnerTexts();
  const before = await page.getByTestId("study-progress").innerText().catch(() => "");
  await typeHuman(page.getByLabel("Study UPI ID"), override ?? upi);
  await typeHuman(page.getByLabel("Study amount"), amount);
  await page.getByRole("button", { name: "Save sample" }).click();
  return before;
}

await page.goto(BASE);
const link = page.getByRole("link", { name: "Volunteer for the typing study" });
await link.waitFor({ timeout: 5000 }).catch(() => {});
ok(await link.isVisible(), "sign-in page links to the study");
await page.getByRole("link", { name: "Volunteer for the typing study" }).click();
await page.getByTestId("study").waitFor();

// Join
await page.getByLabel("New volunteer: invite code").fill(await newInvite());
await page.getByRole("button", { name: "Continue" }).click();
ok(await page.getByText("never sent or stored").isVisible(), "consent text is shown before joining");
ok(await page.getByRole("button", { name: "Join the study" }).isDisabled(), "cannot join without ticking consent");
await page.getByLabel("What are you typing on?").selectOption("laptop");
await page.getByLabel("I have read this and agree to take part").check();
await page.getByRole("button", { name: "Join the study" }).click();
const code = (await page.getByTestId("participant-code").innerText()).trim();
ok(/^[0-9A-Z]{4}-[0-9A-Z]{4}-[0-9A-Z]{4}$/.test(code), `participant code issued (${code})`);
await page.getByLabel("I have saved my code").check();
await page.getByRole("button", { name: "Start typing" }).click();
await page.getByTestId("study-progress").waitFor();

// A wrong prompt is refused in the browser and nothing is sent
await typePrompt("wrong.text@okhdfc");
ok(await page.getByText("Type the sample exactly as shown.").isVisible(), "mistyped prompt is refused");
ok((await page.getByTestId("study-progress").innerText()).includes("sample 1 of 10"), "still on sample 1");

// Full session with human-like typing
for (let i = 0; i < 10; i++) {
  await typePrompt();
  if (i < 9) await page.getByText(`sample ${i + 2} of 10`).waitFor({ timeout: 5000 });
}
await page.getByTestId("study-paused").waitFor();
const paused = await page.getByTestId("study-paused").innerText();
ok(paused.includes("10 samples so far") && paused.includes("next session opens"), "session 1 saved; session 2 waits for the gap");

// Shared laptop: Finish wipes the volunteer, nothing in browser storage
await page.getByRole("button", { name: "Finish (next volunteer)" }).click();
ok(await page.getByLabel("New volunteer: invite code").inputValue() === "", "Finish returns to a blank start page");
const stored = await page.evaluate(() => localStorage.length + sessionStorage.length + document.cookie.length);
ok(stored === 0, "no participant code left in browser storage");

// Resume with the code: still in the gap
await page.getByLabel("Returning volunteer: your participant code").fill(code.toLowerCase());
await page.getByRole("button", { name: "Resume" }).click();
await page.getByTestId("study-paused").waitFor();
ok((await page.getByTestId("study-paused").innerText()).includes("10 samples"), "returning volunteer resumes with their code");

// Withdraw
await page.getByRole("button", { name: "Withdraw" }).click();
await page.getByText("You have left the study").waitFor();
ok(await page.getByText("10 samples were deleted").isVisible(), "withdrawal deletes all 10 samples");
await page.getByRole("button", { name: "Done" }).click();
await page.getByLabel("Returning volunteer: your participant code").fill(code);
await page.getByRole("button", { name: "Resume" }).click();
await page.getByText("not recognised").waitFor();
ok(true, "withdrawn code no longer works");

await browser.close();
console.log(failures ? `\n${failures} FAILED` : "\nALL STUDY BROWSER CHECKS PASSED");
process.exit(failures ? 1 : 0);
