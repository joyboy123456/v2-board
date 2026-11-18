import { chromium } from "playwright";
const b = await chromium.launch({headless:true,executablePath:"/usr/bin/chromium",args:["--no-sandbox"]});
const urls = JSON.parse(process.argv[2]);
const pat = process.argv[3] ? new RegExp(process.argv[3]) : /shots\/|\/sites\/|\/website\/|templates\//;
for (const [name,url,full] of urls) {
  const p = await b.newPage({viewport:{width:1440,height:900},userAgent:"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"});
  try {
    await p.goto(url,{waitUntil:"domcontentloaded",timeout:30000}); await p.waitForTimeout(7000);
    await p.screenshot({path:"refs/"+name+".png", fullPage: !!full});
    const links = await p.$$eval("a", as=>as.map(a=>a.href+" | "+(a.innerText||"").slice(0,60).replace(/\s+/g," ")));
    console.log("==",name,await p.title());
    console.log([...new Set(links.filter(x=>pat.test(x)))].slice(0,40).join("\n"));
  } catch(e){ console.log("ERR",name,e.message.slice(0,200)); }
  await p.close();
}
await b.close();
