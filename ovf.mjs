import { chromium } from "playwright";
const b = await chromium.launch({ headless: true, executablePath: process.env.CHROMIUM_PATH || undefined, args: ["--no-sandbox"] });
const p = await b.newPage({ viewport: { width: 390, height: 844 } });
await p.goto("http://127.0.0.1:3160/"); await p.waitForSelector(".app"); await p.waitForTimeout(800);
console.log(await p.evaluate(() => [...document.querySelectorAll("*")].filter(e => e.getBoundingClientRect().right > innerWidth + 1).slice(0, 12).map(e => e.tagName + "." + e.className + " " + Math.round(e.getBoundingClientRect().right)).join("\n")));
await b.close();
