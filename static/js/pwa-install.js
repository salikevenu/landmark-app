/* Legitimate PWA install invitation.
   Registering/logging in and installing the PWA are two different browser
   mechanisms -- no browser lets a site silently add a home-screen icon.
   This only ever surfaces the browser's own native install flow: it
   listens for beforeinstallprompt, reveals a button, and on click calls
   the captured event's real prompt(). If the browser never fires that
   event (already installed, unsupported browser/platform, or criteria
   not met), the button simply stays hidden -- nothing here fakes or
   forces an install. */
(function () {
  var deferredPrompt = null;
  var btn = document.getElementById('pwaInstallBtn');

  window.addEventListener('beforeinstallprompt', function (event) {
    event.preventDefault();
    deferredPrompt = event;
    if (btn) btn.hidden = false;
  });

  window.addEventListener('appinstalled', function () {
    deferredPrompt = null;
    if (btn) btn.hidden = true;
  });

  if (btn) {
    btn.addEventListener('click', function () {
      if (!deferredPrompt) return;
      var prompted = deferredPrompt;
      deferredPrompt = null;
      btn.hidden = true;
      prompted.prompt();
    });
  }
})();
