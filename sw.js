/* Offline: App-Hülle fest cachen, Daten "network first" mit Cache-Rückfall.
   Am See ohne Empfang zeigt die App damit den letzten Stand statt einer Fehlerseite. */
const HUELLE = "badewasser-huelle-v1";
const DATEN  = "badewasser-daten-v1";
const DATEIEN = ["./", "./index.html", "./manifest.webmanifest"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(HUELLE).then(c => c.addAll(DATEIEN)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys()
      .then(k => Promise.all(k.filter(n => n !== HUELLE && n !== DATEN).map(n => caches.delete(n))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET") return;

  if (url.pathname.endsWith("badestellen.json")) {
    e.respondWith(
      fetch(e.request)
        .then(r => { const k = r.clone(); caches.open(DATEN).then(c => c.put(e.request, k)); return r; })
        .catch(() => caches.match(e.request))
    );
    return;
  }

  if (url.origin === location.origin) {
    e.respondWith(caches.match(e.request).then(r => r || fetch(e.request)));
  }
});
