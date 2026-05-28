// Minimal service worker to enable PWA installability
self.addEventListener('fetch', function(event) {
  // Just pass through all network requests – no offline cache yet
  event.respondWith(fetch(event.request));
});