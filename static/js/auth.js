async function refreshAccessToken() {
  const refreshToken = localStorage.getItem("refresh_token");

  if (!refreshToken) return null;

  const res = await fetch("/api/refresh", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ refresh_token: refreshToken })
  });

  const data = await res.json();

  if (data.access_token) {
    localStorage.setItem("access_token", data.access_token);
    return data.access_token;
  }

  return null;
}