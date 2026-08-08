const SHELL='mm9-mf-v8-1-universe-shell';
const CORE=['./','./index.html','./styles.css','./app.js','./manifest.webmanifest','./logo.png','./icon-192.png','./icon-512.png'];
self.addEventListener('install',e=>{self.skipWaiting();e.waitUntil(caches.open(SHELL).then(c=>c.addAll(CORE)))});
self.addEventListener('activate',e=>{e.waitUntil(Promise.all([self.clients.claim(),caches.keys().then(keys=>Promise.all(keys.filter(k=>k!==SHELL).map(k=>caches.delete(k))))]))});
async function networkFirst(req){try{const r=await fetch(req,{cache:'no-store'});if(r&&r.ok){const c=await caches.open(SHELL);c.put(req,r.clone())}return r}catch(e){return (await caches.match(req))||Response.error()}}
self.addEventListener('fetch',e=>{
  if(e.request.method!=='GET')return;
  const u=new URL(e.request.url);if(u.origin!==self.location.origin)return;
  const isData=u.pathname.includes('/data/');
  const isAppShell=e.request.mode==='navigate'||/\/(index\.html|app\.js|styles\.css|manifest\.webmanifest)$/.test(u.pathname);
  if(isData||isAppShell){e.respondWith(networkFirst(e.request));return}
  e.respondWith(caches.match(e.request).then(cached=>cached||fetch(e.request).then(r=>{const copy=r.clone();caches.open(SHELL).then(c=>c.put(e.request,copy));return r})));
});
