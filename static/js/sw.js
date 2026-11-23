const CACHE_NAME = 'busia-wms-v2';
const ASSETS = [
  '/',
  '/static/css/styles.css',
  '/static/js/main.js',
  '/templates/index.html'
];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE_NAME).then(cache => cache.addAll(ASSETS)));
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(self.clients.claim());
});

self.addEventListener('fetch', e => {
  if(e.request.method !== 'GET') return;
  e.respondWith(
    caches.match(e.request).then(resp => resp || fetch(e.request).catch(()=> caches.match('/')))
  );
});
