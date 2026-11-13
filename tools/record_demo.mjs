// Record the scripted demo (docs/demo.mp4): drives the UI over the Chrome DevTools Protocol.
// Usage: node tools/record_demo.mjs <chrome-headless-shell> <frames-dir>   (Jarvis on :8077)
// Then build the video from <frames-dir>/times.json with ffmpeg (see README).
import { spawn } from "node:child_process"; import fs from "node:fs";
const [,, chrome, dir] = process.argv;
const W = 1280, H = 780, sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const p = spawn(chrome, ["--headless", "--remote-debugging-port=9335", "--no-sandbox", "--use-gl=swiftshader", "about:blank"], { stdio: "ignore" });
let t; for (let i = 0; i < 50 && !t; i++) { try { t = (await (await fetch("http://127.0.0.1:9335/json")).json()).find((x) => x.type === "page"); } catch { await sleep(200); } }
const ws = new WebSocket(t.webSocketDebuggerUrl); await new Promise((r) => (ws.onopen = r));
let id = 0; const pend = new Map(); ws.onmessage = (m) => { const d = JSON.parse(m.data); pend.get(d.id)?.(d); pend.delete(d.id); };
const cdp = (method, params = {}) => new Promise((r) => { const i = ++id; pend.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });
const js = async (e) => (await cdp("Runtime.evaluate", { expression: e, awaitPromise: true, returnByValue: true })).result?.result?.value;
await cdp("Page.enable"); await cdp("Runtime.enable");
await cdp("Emulation.setDeviceMetricsOverride", { width: W, height: H, deviceScaleFactor: 1, mobile: false });
await cdp("Page.navigate", { url: "http://127.0.0.1:8077/" });
for (let i = 0; i < 60 && !(await js(`document.getElementById('health').classList.contains('ok')`)); i++) await sleep(250);
await js(`localStorage.clear(); localStorage.setItem('jarvis.morph','1'); document.getElementById('newChat').click(); true`);

// frame grabber
let recording = true, n = 0; const times = []; const t0 = Date.now();
(async () => { while (recording) { const r = await cdp("Page.captureScreenshot", { format: "jpeg", quality: 82 });
  fs.writeFileSync(`${dir}/f${String(n).padStart(5, "0")}.jpg`, Buffer.from(r.result.data, "base64")); times.push(Date.now() - t0); n++; await sleep(40); } })();

const bots = () => js(`document.querySelectorAll('.msg.bot:not(.pending)').length`);
const waitFor = async (e, ms = 120000) => { const s = Date.now(); while (Date.now() - s < ms) { if (await js(e)) return true; await sleep(150); } return false; };
async function type(q) {
  await js(`document.getElementById('input').focus(); true`);
  for (const ch of q) { await js(`(()=>{const i=document.getElementById('input'); i.value += ${JSON.stringify(ch)}; return true})()`); await sleep(35); }
  await sleep(350);
  const b = await bots();
  await js(`document.getElementById('form').requestSubmit(); true`);
  return b;
}
async function ask(q, hold = 2600) { const b = await type(q); await waitFor(`document.querySelectorAll('.msg.bot:not(.pending)').length > ${b}`); await sleep(hold); }
async function askWithApproval(q, approve) {
  const b = await type(q);
  await waitFor(`!document.getElementById('approval').hidden`); await sleep(2600);
  await js(`document.getElementById('${approve ? "approveBtn" : "rejectBtn"}').click(); true`);
  await waitFor(`document.querySelectorAll('.msg.bot:not(.pending)').length > ${b}`); await sleep(3000);
}

await sleep(2200);
await ask("What IP address does my printer use?");
await ask("When is my next car service due?");
await ask("What is my wife's birthday?");
await askWithApproval("Read my shopping list file, then add the dish soap from it as a task.", true);
await askWithApproval("Add a task to renew my passport, due 2027-01-31, high priority.", false);
await sleep(1500);
recording = false; await sleep(400);
fs.writeFileSync(`${dir}/times.json`, JSON.stringify(times));
console.log("frames:", n, "seconds:", (times.at(-1) / 1000).toFixed(1));
console.log(await js(`[...document.querySelectorAll('.msg')].map(m=>m.innerText.replace(/\\n/g,' ')).join('\\n')`));
ws.close(); p.kill();
