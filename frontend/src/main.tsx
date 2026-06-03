/**
 * Mounts <AIContent> on every DOM element with class "ai-content-mount".
 *
 * Each mount div should have a unique `id`. The raw Markdown content is
 * registered in `window.__AIContentData[id]` by an inline <script> tag
 * placed after the mount div (so tojson works correctly in script context).
 *
 * Auto-mounts on DOMContentLoaded and after every HTMX swap event,
 * so dynamically loaded content gets rendered too.
 * Dispatches "ai-content-mounted" on document after all mounts.
 */

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { AIContent } from "./AIContent";

declare global {
  interface Window {
    __AIContentData?: Record<string, string>;
  }
}

function mountAll(): void {
  let mounted = 0;
  document.querySelectorAll<HTMLElement>(".ai-content-mount").forEach((el) => {
    if ((el as any).__aiMounted) return;
    (el as any).__aiMounted = true;
    mounted++;

    const content =
      window.__AIContentData?.[el.id] || el.getAttribute("data-content") || "";
    if (!content) return;

    const root = createRoot(el);
    root.render(
      <StrictMode>
        <AIContent content={content} />
      </StrictMode>,
    );
  });
  if (mounted > 0) {
    document.dispatchEvent(new CustomEvent("ai-content-mounted"));
  }
}

// Initial mount
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", mountAll);
} else {
  mountAll();
}

// Re-mount when HTMX swaps in new content
document.addEventListener("htmx:afterSettle", mountAll);
