(() => {
  "use strict";
  const INTERVAL_MS = 60000;
  const REQUEST_TIMEOUT_MS = 15000;
  let inFlight = false;

  async function refresh() {
    if (inFlight) return;
    inFlight = true;
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    try {
      const response = await fetch("/", { cache: "no-store", headers: { Accept: "text/html" }, signal: controller.signal });
      if (!response.ok) throw new Error(`Status refresh failed: ${response.status}`);
      const html = await response.text();
      const parsed = new DOMParser().parseFromString(html, "text/html");
      const nextMain = parsed.querySelector("main");
      const currentMain = document.querySelector("main");
      const nextFoot = parsed.querySelector(".foot");
      const currentFoot = document.querySelector(".foot");
      if (!nextMain || !currentMain || !nextFoot || !currentFoot) throw new Error("Incomplete status page");
      currentMain.replaceWith(nextMain);
      currentFoot.replaceWith(nextFoot);
    } catch (error) {
      // Keep the last successfully rendered status; the next timer tick retries.
      console.warn("Status refresh failed; retaining previous data.", error);
    } finally {
      window.clearTimeout(timeout);
      inFlight = false;
    }
  }

  void refresh();
  window.setInterval(refresh, INTERVAL_MS);
})();
