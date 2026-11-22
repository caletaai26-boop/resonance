// End-to-end user journey against a running RESONANCE server (default http://127.0.0.1:8000).
// Usage: node e2e/journey.mjs <path-to-audio> [screenshot-dir]
// Walks the 12 acceptance steps: open, create project, upload, play, analyse, inspect, edit structure,
// edit score, render, compare, export, and view what still needs engines.
import { createRequire } from "node:module";
const { chromium } = createRequire(import.meta.url)("playwright");
import fs from "node:fs";

const BASE = process.env.RESONANCE_URL || "http://127.0.0.1:8000";
const audio = process.argv[2];
const shots = process.argv[3] || "e2e-shots";
if (!audio || !fs.existsSync(audio)) { console.error("Pass the path of an audio file to upload."); process.exit(2); }
fs.mkdirSync(shots, { recursive: true });
const results = [];
const step = async (name, fn) => { const t = Date.now(); try { await fn(); results.push([name, "PASS", Date.now() - t]); console.log(`PASS  ${name}`); } catch (e) { results.push([name, "FAIL", String(e).split("\n")[0]]); console.log(`FAIL  ${name}: ${String(e).split("\n")[0]}`); throw e; } };

const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: 1600, height: 1000 }, deviceScaleFactor: Number(process.env.DSF || 1), acceptDownloads: true });
const page = await ctx.newPage();
const errors = [];
page.on("pageerror", (e) => errors.push(String(e)));
page.on("console", (m) => m.type() === "error" && !m.text().includes("401") && errors.push(m.text()));
const shot = (n) => page.screenshot({ path: `${shots}/${n}.png` });
const idle = async (timeout = 600000) => { // poll the real job table until no agent is queued or running
  const t0 = Date.now();
  for (;;) {
    const n = await page.evaluate(async () => { const id = location.pathname.split("/")[2]; const j = await (await fetch(`/api/projects/${id}/status`)).json(); return j.active_jobs.length; });
    if (n === 0) break;
    if (Date.now() - t0 > timeout) throw new Error("agents still working after timeout");
    await page.waitForTimeout(1500);
  }
  await page.waitForTimeout(1500);
};
const approve = async (n) => { await page.getByText(`Your approval · Gate ${n} of`).first().waitFor({ timeout: 15000 }); await page.getByRole("button", { name: "Approve", exact: true }).click(); await page.getByText(`Gate ${n} approved.`).first().waitFor(); };

try {
  await step("1 open the platform and create an account", async () => {
    await page.goto(BASE);
    await page.getByRole("button", { name: "Create an account" }).click();
    await page.getByLabel("Name").fill("Studio Tester");
    await page.getByLabel("Email").fill(`tester+${Date.now()}@example.org`);
    await page.getByLabel(/Password/).fill("a-long-test-password");
    await page.getByRole("button", { name: "Create account" }).click();
    await page.getByRole("heading", { name: "Project library" }).waitFor();
    await shot("01-dashboard-empty");
  });
  await step("2 create an Adeste Fideles project", async () => {
    await page.getByRole("button", { name: "Create the Adeste Fideles project" }).click();
    await page.getByText("I have the right to upload").click();
    await shot("02-new-project");
    await page.getByRole("button", { name: "Create project" }).click();
    await page.getByText("Awaiting source recording").first().waitFor();
    await shot("03-awaiting-source");
  });
  await step("3 upload the recording", async () => {
    await page.locator('input[type=file]').first().setInputFiles(audio);
    await page.getByText("Recording uploaded").first().waitFor({ timeout: 60000 });
    await page.getByText("Immutable ·").first().waitFor();
  });
  await step("4 play the original", async () => {
    await page.getByRole("button", { name: "Play" }).first().click();
    await page.waitForTimeout(1800);
    const t = await page.evaluate(() => document.querySelector(".font-mono.tabular-nums")?.textContent || "");
    if (!/0:0[1-9]/.test(t)) throw new Error(`playhead did not advance: "${t}"`);
    await page.getByRole("button", { name: "Pause" }).first().click();
  });
  await step("5 run real audio analysis", async () => {
    await page.getByRole("button", { name: "Start analysis" }).click();
    await page.waitForTimeout(2500);
    await shot("04-analysing");
    await idle();
    await page.getByRole("heading", { name: "Technical report" }).waitFor();
  });
  await step("6 inspect the results", async () => {
    await page.getByRole("heading", { name: /Musical findings/ }).waitFor();
    await page.getByRole("button", { name: /Musical Transcription/ }).click();
    await page.getByText("Uncertainty").first().waitFor();
    await shot("05-recording-analysed");
    await approve(1);
  });
  await step("7 review and edit the musical structure", async () => {
    await page.getByRole("link", { name: "Structure" }).click();
    await page.locator('input[list="section-names"]').first().fill("Instrumental introduction");
    await page.locator("tbody select").first().selectOption("instrumental");
    await page.getByRole("button", { name: "Save as new version" }).click();
    await page.getByText(/Saved as (v|version )2/).first().waitFor();
    await shot("06-structure");
    await approve(2);
  });
  await step("8 edit the score (move a note, save a new version)", async () => {
    await page.getByRole("link", { name: "Score" }).click();
    await page.getByLabel("Piano roll editor").waitFor();
    const note = page.locator('[aria-label="Piano roll editor"] g > rect[style*="grab"]').nth(3);
    await note.scrollIntoViewIfNeeded();
    await note.click();
    await page.getByText("Selected note").first().waitFor();
    await page.keyboard.press("ArrowUp");
    await page.getByText("Unsaved edits").first().waitFor();
    await shot("07-score-editor");
    await page.getByPlaceholder("What did you change?").fill("E2E: raised one note a semitone");
    await page.getByRole("button", { name: "Save as new version" }).click();
    await page.getByText(/Saved as version 2/).first().waitFor();
    await page.getByRole("button", { name: "Notation" }).click();
    await page.locator("svg[id^=osmdSvgPage]").first().waitFor({ timeout: 60000 });
    await shot("08-notation");
    await approve(3);
  });
  await step("8b lyrics draft from reference text is labelled unverified", async () => {
    await page.getByRole("link", { name: "Lyrics" }).click();
    await page.getByRole("button", { name: "Load as a draft to correct" }).click();
    await idle(60000);
    await page.getByText("PUBLISHED REFERENCE TEXT").first().waitFor();
    await shot("09-lyrics");
  });
  await step("9 build arrangement and render a genuine instrumental reconstruction", async () => {
    await page.getByRole("link", { name: "Arrangement" }).click();
    await page.getByRole("button", { name: "Build arrangement from the score" }).click();
    await idle(60000);
    await page.getByText("Guide tone, not a choir").first().waitFor();
    await shot("10-arrangement");
    await approve(4);
    await page.getByRole("link", { name: "Render & mix" }).click();
    await page.getByRole("button", { name: "Render, mix, master and inspect" }).click();
    await idle();
    await page.getByText(/Reconstruction v1/).first().waitFor();
    await page.getByText(/Mastered · draft/).first().waitFor();
    await shot("11-render-mix");
  });
  await step("10 compare the reconstruction with the source (A/B + fidelity report)", async () => {
    await page.getByRole("link", { name: "Compare & fidelity" }).click();
    await page.getByText("Fidelity report").first().waitFor();
    await page.getByText("does not certify").first().waitFor();
    await page.getByRole("button", { name: "Play" }).first().click();
    await page.waitForTimeout(1500);
    await page.keyboard.press("b");
    await page.waitForTimeout(1500);
    const bTime = await page.evaluate(() => [...document.querySelectorAll("span.font-mono")].map((e) => e.textContent).join("|"));
    await page.keyboard.press("a");
    await page.keyboard.press(" ");
    await shot("12-compare");
    if (!/0:0[2-9]/.test(bTime)) throw new Error(`B did not continue from A's position: ${bTime}`);
    await approve(5);
    await approve(6);
  });
  await step("11 export the generated audio (draft, then final after Gate 7)", async () => {
    await page.getByRole("link", { name: "Export" }).click();
    await page.getByText("Draft status.").first().waitFor();
    const row = page.locator("div").filter({ hasText: /^WAV master \(24-bit\)/ }).last();
    let [dl] = await Promise.all([page.waitForEvent("download"), page.locator('a[href$="/export/master_wav"]').click()]);
    if (!dl.suggestedFilename().includes("DRAFT")) throw new Error(`expected DRAFT filename, got ${dl.suggestedFilename()}`);
    await approve(7);
    await page.getByText("Final master authorised.").first().waitFor();
    [dl] = await Promise.all([page.waitForEvent("download"), page.locator('a[href$="/export/master_mp3"]').click()]);
    const p = await dl.path();
    if (!dl.suggestedFilename().includes("FINAL") || fs.statSync(p).size < 50000) throw new Error(`bad final export ${dl.suggestedFilename()}`);
    await shot("13-export");
  });
  await step("12 see which advanced features still need engines", async () => {
    await page.getByText("What still needs additional engines or services").first().waitFor();
    await page.getByText("Latin SATB choir engine").first().waitFor();
    await page.getByRole("link", { name: "Engines & settings" }).click();
    await page.getByText("Audio and music engines").first().waitFor();
    await shot("14-settings");
  });
  await step("13 editing the approved score re-opens dependent gates", async () => {
    await page.goBack();
    await page.getByRole("link", { name: "Score" }).click();
    const note = page.locator('[aria-label="Piano roll editor"] g > rect[style*="grab"]').nth(5);
    await note.scrollIntoViewIfNeeded(); await note.click();
    await page.keyboard.press("ArrowDown");
    await page.getByRole("button", { name: "Save as new version" }).click();
    await page.getByText(/Gate 3, 4, 5, 6, 7 re-opened/).first().waitFor();
    await shot("15-invalidation");
  });
} catch { await shot("zz-failure").catch(() => {}); }
await browser.close();
console.log("\nconsole errors:", errors.length ? errors.slice(0, 8) : "none");
const failed = results.filter((r) => r[1] === "FAIL").length;
console.log(`${results.length - failed}/${results.length} steps passed`);
process.exit(failed || errors.length ? 1 : 0);
