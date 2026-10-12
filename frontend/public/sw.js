// frontend/public/sw.js — the service-worker kill switch.
//
// History: an early CampusQuest build registered a precaching service
// worker under this scope. That deployment is gone (the app ships no
// worker today), but browsers holding the old registration keep serving
// its stale shell ("古早界面" + failing API calls) because a 404 on the
// update check leaves the installed worker in control.
//
// This file breaks that loop for every legacy visitor: on install it
// takes over immediately, on activate it deletes every cache it can see
// and unregisters itself. One navigation later the browser is on the
// live shell with no worker at all.
//
// Do NOT add caching logic here. If the product later wants a real
// offline PWA, ship a versioned worker with an update flow and remove
// this switch in the same release.
self.addEventListener("install", () => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const names = await caches.keys();
      await Promise.all(names.map((name) => caches.delete(name)));
      await self.registration.unregister();
    })(),
  );
});
