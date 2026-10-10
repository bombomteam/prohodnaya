// Optional QA: serve demo locally; start Edge with a separate temporary profile,
// --remote-debugging-port=9223 --use-fake-device-for-media-stream
// --use-fake-ui-for-media-stream. Physical camera and backend are not tested.
import assert from 'node:assert/strict';
const port = process.env.CDP_PORT || '9223';
const appUrl = process.env.QA_URL || 'http://localhost:8080/';
assert.ok(['localhost', '127.0.0.1'].includes(new URL(appUrl).hostname));
const sleep = ms => new Promise(r => setTimeout(r, ms));
const tabs = [];
async function tab(url) {
  const t = await fetch(`http://127.0.0.1:${port}/json/new?${url}`, {method:'PUT'}).then(r => r.json());
  const ws = new WebSocket(t.webSocketDebuggerUrl);
  await new Promise(r => ws.onopen = r);
  let id = 0; const pending = new Map(); const errors = [];
  ws.onmessage = e => {
    const m = JSON.parse(e.data);
    if(m.method === 'Runtime.exceptionThrown') errors.push(m.params.exceptionDetails);
    if(!m.id) return;
    const p = pending.get(m.id); pending.delete(m.id);
    m.error ? p.reject(m.error) : p.resolve(m.result);
  };
  const send = (method, params={}) => new Promise((resolve,reject) => { const n=++id; pending.set(n,{resolve,reject}); ws.send(JSON.stringify({id:n,method,params})); });
  const evaluate = async expression => {
    const r = await send('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});
    if(r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails));
    return r.result.value;
  };
  const result={send,evaluate,errors,ws,t}; tabs.push(result);
  await send('Runtime.enable'); await sleep(900); return result;
}
const select = (id,value) => `(()=>{const s=document.querySelector('#${id}');s.value=${JSON.stringify(value)};s.dispatchEvent(new Event('change',{bubbles:true}));})()`;
try {
  const gate=await tab(appUrl);
  const employeeId = await gate.evaluate(`(async()=>{
    const {demoApi}=await import('./src/demo-api.js');
    const f=new FormData();f.set('name','Рабочий для браузерной проверки');
    f.set('referencePhoto',new File(['demo'],'face.jpg'));
    const {employee}=await demoApi.saveEmployee(null,f);await demoApi.enroll(employee.id);
    return employee.id;
  })()`);
  await gate.evaluate(select('demo-role','controller')); await sleep(300);
  await gate.evaluate(select('gate-person-select',employeeId));
  await gate.evaluate(`document.querySelector('[data-action="create-challenge"]').click()`); await sleep(300);
  assert.ok(await gate.evaluate(`!!document.querySelector('#gate-code-form')`));
  const worker=await tab(new URL('?worker=1', appUrl).href);
  await worker.evaluate(select('worker-select',employeeId)); await sleep(300);
  await worker.evaluate(`document.querySelector('[data-action="worker-get-code"]').click()`); await sleep(300);
  const code=await worker.evaluate(`document.querySelector('.worker-pass-code')?.textContent`);
  assert.match(code,/^\d{6}$/);
  await gate.evaluate(`(()=>{const f=document.querySelector('#gate-code-form'); f.elements.code.value=${JSON.stringify(code)};f.requestSubmit()})()`); await sleep(300);
  assert.equal(await gate.evaluate(`document.querySelector('[data-action="start-camera"]').disabled`),false);
  await gate.evaluate(`document.querySelector('[data-action="start-camera"]').click()`); await sleep(1200);
  await gate.evaluate(`document.querySelector('[data-action="capture"]').click()`); await sleep(1800);
  assert.ok(await gate.evaluate(`!!document.querySelector('.camera-box img')`));
  await gate.evaluate(`document.querySelector('[data-action="decide-pass"]').click()`); await sleep(300);
  assert.equal(await gate.evaluate(`document.querySelector('.result-card strong')?.textContent`),'Проход разрешён');
  await sleep(2200);
  assert.equal(await worker.evaluate(`document.querySelector('.shift-card strong')?.textContent`),'Смена открыта');
  assert.equal(await worker.evaluate(`!!document.querySelector('.worker-pass-code')`),false);
  for(const width of [320,390,768,1280]) for(const page of [gate,worker]) {
    await page.send('Emulation.setDeviceMetricsOverride',{width,height:844,deviceScaleFactor:1,mobile:width<768});
    const layout=await page.evaluate('({width:innerWidth,scroll:document.documentElement.scrollWidth})');
    assert.ok(layout.scroll<=layout.width+1,JSON.stringify(layout));
  }
  assert.deepEqual(tabs.flatMap(t=>t.errors),[]);
  console.log('PASS: two-tab employee code, controller verification, fake camera capture, shift status and 320/390/768/1280 layouts');
} finally {
  for(const tab of tabs) { tab.ws.close(); await fetch(`http://127.0.0.1:${port}/json/close/${tab.t.id}`).catch(()=>{}); }
}
