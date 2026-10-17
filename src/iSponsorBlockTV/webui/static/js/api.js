/** The calls to the web UI server. Every function rejects with an Error that has a message. */

// Sent with every POST, the server rejects requests without it (CSRF protection).
const POST_HEADERS = {
  "Content-Type": "application/json",
  "X-Requested-With": "iSponsorBlockTV",
};

async function request(path, body) {
  const options =
    body === undefined
      ? {}
      : { method: "POST", headers: POST_HEADERS, body: JSON.stringify(body) };
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.error || `Error ${response.status}`);
  }
  return data;
}

export const loadConfig = () => request("/api/config");
export const saveConfig = (config) => request("/api/config", config);
export const pairDevice = (code) => request("/api/pair", { code });
export const restartProgram = () => request("/api/restart", {});
