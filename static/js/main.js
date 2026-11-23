// register service worker
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/static/js/sw.js').then(()=> console.log('SW registered'));
}

// IndexedDB helper (simple)
const DB_NAME = 'busia_wms_db';
const DB_VERSION = 1;
let db;
function openDb(){
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = e => {
      const idb = e.target.result;
      if(!idb.objectStoreNames.contains('queue')) idb.createObjectStore('queue', { keyPath: 'id', autoIncrement: true });
    };
    req.onsuccess = e => { db = e.target.result; resolve(db); };
    req.onerror = e => reject(e);
  });
}
async function addToQueue(action, payload){
  if(!db) await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction('queue','readwrite');
    const store = tx.objectStore('queue');
    store.add({ action, payload, ts: Date.now() });
    tx.oncomplete = ()=> resolve(true);
    tx.onerror = e => reject(e);
  });
}
async function getQueue(){
  if(!db) await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction('queue','readonly');
    const store = tx.objectStore('queue');
    const req = store.getAll();
    req.onsuccess = ()=> resolve(req.result);
    req.onerror = e => reject(e);
  });
}
async function removeFromQueue(id){
  if(!db) await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction('queue','readwrite');
    const store = tx.objectStore('queue');
    store.delete(id);
    tx.oncomplete = ()=> resolve(true);
    tx.onerror = e => reject(e);
  });
}

// sync queue when online
window.addEventListener('online', syncQueue);
async function syncQueue(){
  const q = await getQueue();
  for(const item of q){
    try{
      const url = item.action === 'receive' ? '/api/receive' : '/api/issue';
      const res = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(item.payload)});
      const j = await res.json();
      if(j.status === 'ok'){
        await removeFromQueue(item.id);
      } else {
        console.warn('Sync error', j.message);
        break;
      }
    } catch(err){
      console.warn('Sync failed', err);
      break;
    }
  }
  if(navigator.onLine) location.reload();
}

// helper to queue or send
async function postOrQueue(action, payload){
  if(!navigator.onLine){
    await addToQueue(action, payload);
    return { queued: true };
  }
  const url = action === 'receive' ? '/api/receive' : '/api/issue';
  const res = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
  return res.json();
}

/* Barcode camera scanning integration (html5-qrcode)
   Usage: include a <div id="reader"></div> and call startScanner(callback)
   Add CDN in templates: <script src="https://unpkg.com/html5-qrcode"></script>
*/
let html5QrcodeScanner;
function startScanner(onDetected){
  if(typeof Html5Qrcode === 'undefined') { console.warn('html5-qrcode not loaded'); return; }
  const readerId = "reader";
  if(!document.getElementById(readerId)) return;
  html5QrcodeScanner = new Html5Qrcode(readerId);
  Html5Qrcode.getCameras().then(cameras => {
    const cameraId = cameras && cameras.length ? cameras[0].id : null;
    html5QrcodeScanner.start(
      cameraId,
      { fps: 10, qrbox: 250 },
      (decodedText, decodedResult) => { onDetected(decodedText); },
      (errorMessage) => { /* ignore scan errors */ }
    ).catch(err => console.error(err));
  }).catch(err => console.error(err));
}
function stopScanner(){
  if(html5QrcodeScanner) html5QrcodeScanner.stop().then(()=> html5QrcodeScanner.clear()).catch(()=>{});
}
