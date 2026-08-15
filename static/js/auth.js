/* Cookie-session refresh helper. Tokens are not stored in localStorage. */
async function refreshAccessToken() {
  if (!window.LandmarkSession) return false;
  return LandmarkSession.tryRefresh();
}
