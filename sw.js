// Uygulama kabuğu önbellekten, veri ağdan (ağ yoksa son kaydedilen veri)
const CACHE = 'altin-v2';
const SHELL = ['./', 'index.html', 'manifest.webmanifest', 'icon-180.png', 'icon-192.png', 'icon-512.png'];
self.addEventListener('install', e => { e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting())) });
self.addEventListener('activate', e => { e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim())) });
self.addEventListener('fetch', e => {
  const u = new URL(e.request.url);
  if (e.request.method !== 'GET' || u.origin !== location.origin) return;
  // Her şey önce ağdan; başarılıysa önbelleği güncelle, ağ yoksa önbellekten
  const key = u.pathname.includes('/data/') ? new Request(u.origin + u.pathname) : e.request;
  e.respondWith(fetch(e.request).then(r => { if (r.ok){ const c = r.clone(); caches.open(CACHE).then(x => x.put(key, c)) } return r })
    .catch(() => caches.match(key).then(r => r || caches.match('index.html'))));
});
