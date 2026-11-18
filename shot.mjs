import { chromium } from "playwright";
const base = process.argv[2] || "http://127.0.0.1:3160/";
const out = process.argv[3] || "design/shots";
const b = await chromium.launch({ headless: true, executablePath: process.env.CHROMIUM_PATH || undefined, args: ["--no-sandbox"] });
const errs = [];
for (const [name, w, h, q, fullPage] of [
  ["desk", 1440, 900, "", false],
  ["desk-full", 1440, 900, "", true],
  ["desk-drawer", 1440, 900, "?open=h/TRUNK", false],
  ["desk-focus", 1440, 900, "?focus=parity/G1", false],
  ["mobile-focus", 390, 844, "?focus=parity/G1", false],
  ["mobile", 390, 844, "", false],
  ["mobile-full", 390, 844, "", true],
]) {
  const p = await b.newPage({ viewport: { width: w, height: h }, deviceScaleFactor: 2 });
  p.on("console", m => m.type() === "error" && errs.push(name + ": " + m.text()));
  p.on("pageerror", e => errs.push(name + ": " + e.message));
  await p.goto(base + q, { waitUntil: "domcontentloaded" });
  await p.waitForSelector(".app", { timeout: 15000 });
  await p.waitForTimeout(1200);
  if (q.includes("focus")) { await p.evaluate(() => document.querySelector(".mapp").scrollIntoView()); await p.waitForTimeout(300); }
  const overflow = await p.evaluate(() => document.documentElement.scrollWidth > innerWidth);
  if (overflow) errs.push(name + ": 横向溢出 ");
  await p.screenshot({ path: `${out}/${name}.png`, fullPage });
  await p.close();
}
await b.close();
console.log(errs.length ? errs.join("\n") : "NO_ERRORS");
