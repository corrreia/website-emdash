/**
 * Cloudflare AI Search query endpoint.
 *
 * The plugin ships the handler; this route just exposes it at a stable URL so
 * the search UI has something to POST to. Keep the path in sync with the
 * `apiUrl` passed to <AISearchSnippet> in the layout.
 *
 * Requires the `AI_SEARCH` namespace binding from wrangler.jsonc. Without it
 * the handler has no namespace to resolve and returns no results, which is the
 * expected state in local development.
 */
export { POST, prerender } from "@emdash-cms/cloudflare/plugins/ai-search";
