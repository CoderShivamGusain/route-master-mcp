// Where the route-master server runs. Change SERVER_URL if you host your own copy
// (and add that host to "host_permissions" in manifest.json).
export const SERVER_URL = "https://mcp-route-master.onrender.com";
export const VERSION = chrome.runtime.getManifest().version;
// Only these sites can be opened or touched.
export const ALLOWED_HOSTS = [/(^|\.)redbus\.in$/, /(^|\.)makemytrip\.com$/];
