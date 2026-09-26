// Fixed public tool surface for InfluencerResearch.
// The MCP server exposes the same allowlist; the gateway is a second, narrow fence.
export const ALLOWED_TOOLS = new Set([
  'creator_list',
  'creator_get',
  'creator_register',
  'creator_evaluate',
  'creator_monitor',
  'creator_recent_check',
  'research_status',
  'research_stop',
]);

export function checkRequest(msg) {
  if (msg?.method !== 'tools/call') return {};
  const name = msg.params?.name;
  if (!ALLOWED_TOOLS.has(name)) {
    return { error: `Tool not available on this public endpoint: ${name ?? '(missing)'}` };
  }
  return {};
}

export function rewriteResponse(msg) {
  if (msg?.result?.tools) {
    msg.result.tools = msg.result.tools.filter(tool => ALLOWED_TOOLS.has(tool.name));
  }
  return msg;
}

export const rpcError = (id, message) => ({
  jsonrpc: '2.0',
  id: id ?? null,
  error: { code: -32601, message },
});
