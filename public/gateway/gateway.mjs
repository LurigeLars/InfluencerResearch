// Cloudflare Access gatekeeper for InfluencerResearch MCP.
// Network path: cloudflared -> gateway:8080 -> influencerresearch:8770/mcp.
// Node standard library only.
import http from 'node:http';
import crypto from 'node:crypto';
import { checkRequest, rewriteResponse, rpcError } from './policy.mjs';

const UPSTREAM_HOST = 'influencerresearch';
const UPSTREAM_PORT = 8770;
const UPSTREAM_PATH = '/mcp';
const PORT = 8080;
const RATE_PER_MIN = Number(process.env.RATE_PER_MIN ?? 120);
const MAX_BODY = 512 * 1024;

const ACCESS_TEAM_DOMAIN = process.env.ACCESS_TEAM_DOMAIN ?? '';
const ACCESS_AUD = process.env.ACCESS_AUD ?? '';
const ACCESS_EMAILS = new Set(
  (process.env.ACCESS_ALLOWED_EMAILS ?? '')
    .split(',')
    .map(value => value.trim().toLowerCase())
    .filter(Boolean),
);

if (!/^[a-z0-9-]+\.cloudflareaccess\.com$/i.test(ACCESS_TEAM_DOMAIN)) {
  console.error('ACCESS_TEAM_DOMAIN must be a Cloudflare Access team domain (*.cloudflareaccess.com)');
  process.exit(1);
}
if (!ACCESS_AUD || ACCESS_AUD.length > 512 || /\s/.test(ACCESS_AUD)) {
  console.error('ACCESS_AUD is required and must be one non-whitespace audience value');
  process.exit(1);
}
if (!Number.isInteger(RATE_PER_MIN) || RATE_PER_MIN < 1 || RATE_PER_MIN > 10000) {
  console.error('RATE_PER_MIN must be an integer between 1 and 10000');
  process.exit(1);
}

const ACCESS_ISSUER = `https://${ACCESS_TEAM_DOMAIN}`;
const jwks = { keys: new Map(), fetchedAt: 0 };

async function accessKey(kid) {
  const stale = Date.now() - jwks.fetchedAt > 3_600_000;
  const canRefresh = Date.now() - jwks.fetchedAt > 30_000;
  if ((stale || !jwks.keys.has(kid)) && canRefresh) {
    jwks.fetchedAt = Date.now();
    const response = await fetch(
      `${ACCESS_ISSUER}/cdn-cgi/access/certs`,
      { signal: AbortSignal.timeout(5000) },
    );
    if (!response.ok) throw new Error(`certs HTTP ${response.status}`);
    const payload = await response.json();
    const keys = Array.isArray(payload?.keys) ? payload.keys : [];
    jwks.keys = new Map(
      keys.map(key => [key.kid, crypto.createPublicKey({ key, format: 'jwk' })]),
    );
  }
  return jwks.keys.get(kid);
}

function decodeJson(segment) {
  return JSON.parse(Buffer.from(segment, 'base64url').toString('utf8'));
}

async function verifyAccessJwt(token) {
  const parts = String(token ?? '').split('.');
  if (parts.length !== 3) return { ok: false, reason: 'missing token' };

  try {
    const header = decodeJson(parts[0]);
    const claims = decodeJson(parts[1]);
    if (header.alg !== 'RS256') return { ok: false, reason: 'wrong algorithm' };

    const key = await accessKey(header.kid);
    if (!key) return { ok: false, reason: 'unknown key id' };

    const valid = crypto.verify(
      'RSA-SHA256',
      Buffer.from(`${parts[0]}.${parts[1]}`),
      key,
      Buffer.from(parts[2], 'base64url'),
    );
    if (!valid) return { ok: false, reason: 'bad signature' };

    const now = Date.now() / 1000;
    const audiences = [claims.aud].flat();
    if (!audiences.includes(ACCESS_AUD)) return { ok: false, reason: 'wrong audience' };
    if (claims.iss !== ACCESS_ISSUER) return { ok: false, reason: 'wrong issuer' };
    if (typeof claims.exp !== 'number' || claims.exp < now - 30) return { ok: false, reason: 'expired' };
    if (typeof claims.nbf === 'number' && claims.nbf > now + 30) return { ok: false, reason: 'not yet valid' };

    const email = String(claims.email ?? '').toLowerCase();
    if (ACCESS_EMAILS.size && !ACCESS_EMAILS.has(email)) {
      return { ok: false, reason: 'identity not allowlisted' };
    }

    const identity = email || String(claims.sub ?? claims.common_name ?? 'access-identity');
    return { ok: true, identity };
  } catch (error) {
    return { ok: false, reason: 'verification failed' };
  }
}

const windows = new Map();

function rateLimited(identity) {
  const now = Date.now();
  const current = windows.get(identity);
  if (!current || now - current.start >= 60_000) {
    windows.set(identity, { start: now, count: 1 });
    return false;
  }
  current.count += 1;
  return current.count > RATE_PER_MIN;
}

setInterval(() => {
  const now = Date.now();
  for (const [identity, value] of windows) {
    if (now - value.start >= 60_000) windows.delete(identity);
  }
}, 60_000).unref();

function send(res, status, body = '') {
  res.writeHead(status, { 'content-type': 'text/plain; charset=utf-8' });
  res.end(body);
}

function sendJson(res, value) {
  res.writeHead(200, { 'content-type': 'application/json' });
  res.end(JSON.stringify(value));
}

function rewriteJson(text) {
  try {
    const parsed = JSON.parse(text);
    return JSON.stringify(
      Array.isArray(parsed)
        ? parsed.map(message => rewriteResponse(message))
        : rewriteResponse(parsed),
    );
  } catch {
    return text;
  }
}

function forward(req, res, body, rewrite = false) {
  const headers = {
    ...req.headers,
    host: `${UPSTREAM_HOST}:${UPSTREAM_PORT}`,
  };

  delete headers['content-length'];
  delete headers.authorization;
  delete headers['x-api-key'];
  delete headers['cf-access-jwt-assertion'];
  delete headers.cookie;
  delete headers.origin;
  delete headers.referer;

  if (body) headers['content-length'] = String(body.length);

  const upstream = http.request(
    {
      host: UPSTREAM_HOST,
      port: UPSTREAM_PORT,
      method: req.method,
      path: UPSTREAM_PATH,
      headers,
    },
    upstreamResponse => {
      if (!rewrite) {
        res.writeHead(upstreamResponse.statusCode ?? 502, upstreamResponse.headers);
        upstreamResponse.pipe(res);
        return;
      }

      const chunks = [];
      upstreamResponse.on('data', chunk => chunks.push(chunk));
      upstreamResponse.on('end', () => {
        const output = Buffer.from(
          rewriteJson(Buffer.concat(chunks).toString('utf8')),
        );
        const responseHeaders = { ...upstreamResponse.headers };
        delete responseHeaders['transfer-encoding'];
        responseHeaders['content-length'] = String(output.length);
        res.writeHead(upstreamResponse.statusCode ?? 502, responseHeaders);
        res.end(output);
      });
    },
  );

  upstream.on('error', () => {
    if (!res.headersSent) send(res, 502, 'upstream unavailable');
    else res.destroy();
  });

  res.on('close', () => {
    if (!res.writableFinished) upstream.destroy();
  });

  upstream.end(body);
}

function isLocalHealthRequest(req) {
  const address = req.socket.remoteAddress ?? '';
  return (
    req.method === 'GET' &&
    req.url === '/healthz' &&
    (address === '127.0.0.1' || address === '::1' || address === '::ffff:127.0.0.1')
  );
}

http.createServer(async (req, res) => {
  if (isLocalHealthRequest(req)) return send(res, 200, 'ok');

  const path = new URL(req.url ?? '/', 'http://gateway').pathname;
  if (path !== '/mcp') return send(res, 404);
  if (!['GET', 'POST', 'DELETE'].includes(req.method ?? '')) return send(res, 405);

  const access = await verifyAccessJwt(req.headers['cf-access-jwt-assertion']);
  if (!access.ok) return send(res, 403, 'forbidden');
  if (rateLimited(access.identity)) return send(res, 429, 'rate limited');

  if (req.method !== 'POST') return forward(req, res, null, false);

  const chunks = [];
  let size = 0;
  let rejected = false;

  req.on('data', chunk => {
    if (rejected) return;
    size += chunk.length;
    if (size > MAX_BODY) {
      rejected = true;
      send(res, 413, 'request too large');
      req.destroy();
      return;
    }
    chunks.push(chunk);
  });

  req.on('end', () => {
    if (rejected) return;
    const body = Buffer.concat(chunks);

    let messages;
    try {
      messages = [JSON.parse(body.toString('utf8'))].flat();
    } catch {
      return send(res, 400, 'invalid json');
    }

    let rewrite = false;
    for (const message of messages) {
      if (message?.method === 'tools/list') rewrite = true;
      const verdict = checkRequest(message);
      if (verdict.error) return sendJson(res, rpcError(message.id, verdict.error));
    }

    forward(req, res, body, rewrite);
  });
}).listen(PORT, '0.0.0.0', () => {
  console.log(`InfluencerResearch gateway listening on ${PORT}; Cloudflare Access required`);
});
