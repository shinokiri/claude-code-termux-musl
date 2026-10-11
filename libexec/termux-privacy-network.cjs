'use strict';

// SPDX-License-Identifier: MIT
// Request suppression for known optional collectors, beneath producer controls.
// This is a compatibility hook, not an OS network sandbox.
const http = require('node:http');
const https = require('node:https');
const { Writable, Readable, Duplex } = require('node:stream');
const { syncBuiltinESMExports } = require('node:module');
const http2 = require('node:http2');
const { EventEmitter } = require('node:events');

const installed = Symbol.for('claude-termux.privacy-network.v1');
let count = () => {};
const intakeHosts = new Set([
  'http-intake.logs.us5.datadoghq.com',
  'browser-intake-us5-datadoghq.com',
]);
const apiHosts = new Set(['api.anthropic.com', 'api-staging.anthropic.com']);

function classify(value) {
  let url;
  try { url = new URL(value); } catch { return null; }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') return null;
  const host = url.hostname.toLowerCase().replace(/\.$/, '');
  if (intakeHosts.has(host)) return { host, path: url.pathname, kind: 'datadog' };
  if (!apiHosts.has(host)) return null;
  if (url.pathname === '/api/event_logging' || url.pathname.startsWith('/api/event_logging/')) {
    return { host, path: url.pathname, kind: 'events' };
  }
  if (url.pathname === '/api/claude_code/metrics' || url.pathname === '/api/claude_code/metrics/') {
    return { host, path: url.pathname, kind: 'metrics' };
  }
  if (url.pathname === '/api/claude_cli_feedback') return { host, path: url.pathname, kind: 'feedback' };
  if (/^\/v1\/code\/sessions\/[^/]+\/worker\/diagnostics$/.test(url.pathname)) return { host, path: url.pathname, kind: 'diagnostics' };
  return null;
}

function requestDetails(args, protocol) {
  const first = args[0];
  let options = {};
  if (typeof first === 'string' || first instanceof URL) {
    try {
      const url = new URL(first);
      options = { protocol: url.protocol, hostname: url.hostname, port: url.port,
        path: url.pathname + url.search, method: 'GET' };
    } catch { return null; }
    if (args[1] && typeof args[1] === 'object') Object.assign(options, args[1]);
  } else if (first && typeof first === 'object') {
    options = { ...first };
  } else return null;
  const path = options.path || '/';
  // HTTP forward proxies put the destination URL in the request path.
  let url;
  if (/^https?:\/\//i.test(path)) url = path;
  else {
    let host = options.hostname || options.host || 'localhost';
    if (host.includes(':') && !host.startsWith('[') && !/^[^:]+:\d+$/.test(host)) host = '[' + host + ']';
    url = `${options.protocol || protocol}//${host}${options.port ? ':' + options.port : ''}${path}`;
  }
  const match = classify(url);
  return match ? { ...match, options, callback: args.findLast(arg => typeof arg === 'function') } : null;
}

function record(match, transport) { count('network-' + transport + '-' + match.kind); }

class SuppressedRequest extends Writable {
  constructor(details) {
    super({ autoDestroy: false });
    this.method = details.options.method || 'GET';
    this.path = details.options.path || '/';
    this.protocol = details.options.protocol;
    this.host = details.options.hostname || details.options.host;
    this.agent = details.options.agent;
    this.res = null;
    this.socket = null;
    this.aborted = false;
    this.headers = new Map();
    for (const [name, value] of Object.entries(details.options.headers || {})) this.setHeader(name, value);
    if (details.callback) this.once('response', details.callback);
    this.once('finish', () => queueMicrotask(() => {
      if (this.destroyed || this.aborted) return;
      const response = Readable.from([Buffer.from('{}')]);
      response.statusCode = details.kind === 'datadog' ? 202 : 200;
      response.statusMessage = details.kind === 'datadog' ? 'Accepted' : 'OK';
      response.headers = { 'content-type': 'application/json', 'content-length': '2' };
      response.rawHeaders = ['Content-Type', 'application/json', 'Content-Length', '2'];
      response.trailers = {};
      response.rawTrailers = [];
      response.httpVersion = '1.1';
      response.complete = true;
      response.req = this;
      this.res = response;
      this.emit('response', response);
      response.once('end', () => this.emit('close'));
    }));
  }
  _write(_chunk, _encoding, callback) { callback(); }
  _writev(_chunks, callback) { callback(); }
  setHeader(name, value) { this.headers.set(String(name).toLowerCase(), value); return this; }
  getHeader(name) { return this.headers.get(String(name).toLowerCase()); }
  hasHeader(name) { return this.headers.has(String(name).toLowerCase()); }
  getHeaders() { return Object.fromEntries(this.headers); }
  getHeaderNames() { return [...this.headers.keys()]; }
  removeHeader(name) { this.headers.delete(String(name).toLowerCase()); }
  flushHeaders() {}
  setTimeout(_timeout, callback) { if (callback) this.once('timeout', callback); return this; }
  setNoDelay() { return this; }
  setSocketKeepAlive() { return this; }
  abort() { this.aborted = true; this.emit('abort'); this.destroy(); }
}

class SuppressedHttp2Stream extends Duplex {
  constructor(session, match, options = {}) {
    super();
    this.session = session;
    this.match = match;
    this.rstCode = 0;
    this.sentHeaders = {};
    if (options.endStream) queueMicrotask(() => this.end());
    if (options.signal) {
      const abort = () => this.destroy(Object.assign(new Error('The operation was aborted'), { name: 'AbortError', code: 'ABORT_ERR' }));
      if (options.signal.aborted) queueMicrotask(abort);
      else { options.signal.addEventListener('abort', abort, { once: true });
        this.once('close', () => options.signal.removeEventListener('abort', abort)); }
    }
  }
  _read() {}
  _write(_chunk, _encoding, callback) { callback(); }
  _final(callback) {
    callback();
    queueMicrotask(() => {
      if (this.destroyed) return;
      this.emit('response', { ':status': this.match.kind === 'datadog' ? 202 : 200, 'content-type': 'application/json' }, 0);
      this.push(Buffer.from('{}'));
      this.push(null);
    });
  }
  close(code = 0, callback) { this.rstCode = code; if (callback) this.once('close', callback); this.destroy(); }
  setTimeout() { return this; }
  priority() {}
}

class SuppressedSession extends EventEmitter {
  constructor(base) { super(); this.base = base; this.closed = false; this.destroyed = false; }
  request(headers = {}, options) {
    const match = classify(this.base) || { kind: 'datadog' };
    return new SuppressedHttp2Stream(this, match, options);
  }
  close(callback) { this.closed = true; if (callback) this.once('close', callback); queueMicrotask(() => this.emit('close')); }
  destroy() { this.destroyed = true; this.close(); }
  ref() { return this; }
  unref() { return this; }
}

function installNetworkPolicy(counter, rebind) {
  count = counter || count;
  if (globalThis[installed]) return false;
  globalThis[installed] = true;
  const originalFetch = globalThis.fetch;
  if (typeof originalFetch === 'function') {
    globalThis.fetch = function(input, options) {
      let url;
      try { url = typeof input === 'string' || input instanceof URL ? String(input) : input?.url; } catch {}
      const match = classify(url);
      if (!match) return Reflect.apply(originalFetch, this, arguments);
      record(match, 'fetch');
      return Promise.resolve(new Response('{}', {
        status: match.kind === 'datadog' ? 202 : 200,
        headers: { 'content-type': 'application/json' },
      }));
    };
  }
  for (const [module, protocol] of [[http, 'http:'], [https, 'https:']]) {
    const originalRequest = module.request;
    module.request = function(...args) {
      const details = requestDetails(args, protocol);
      if (!details) return Reflect.apply(originalRequest, this, args);
      record(details, protocol.slice(0, -1));
      return new SuppressedRequest(details);
    };
    module.get = function(...args) { const request = module.request(...args); request.end(); return request; };
  }
  const originalConnect = http2.connect;
  http2.connect = function(authority, options, listener) {
    const base = String(authority);
    // Intake-only hosts need no network connection. API hosts can also carry
    // business requests, so their sessions retain ordinary connection behavior.
    const intake = classify(base);
    const session = intake?.kind === 'datadog' ? new SuppressedSession(base) :
      Reflect.apply(originalConnect, this, arguments);
    const request = session.request.bind(session);
    session.request = function(headers = {}, settings) {
      let origin;
      try { origin = new URL(base); } catch { return request(headers, settings); }
      const host = headers[':authority'] || headers.host || origin.host;
      const match = classify(`${headers[':scheme'] || origin.protocol.slice(0, -1)}://${host}${headers[':path'] || '/'}`);
      if (!match) return request(headers, settings);
      record(match, 'http2');
      return new SuppressedHttp2Stream(session, match, settings);
    };
    if (session instanceof SuppressedSession) {
      const callback = typeof options === 'function' ? options : listener;
      if (callback) session.once('connect', callback);
      queueMicrotask(() => session.emit('connect', session, {}));
    }
    return session;
  };
  if (typeof syncBuiltinESMExports === 'function') syncBuiltinESMExports();
  if (rebind) for (const [name, module] of [['http', http], ['https', https], ['http2', http2]]) {
    // Bun can pre-link a later preload's named imports before awaiting this
    // preload. Refresh those bindings as well as the default module object.
    for (const path of [name, 'node:' + name]) rebind(path, () => ({ ...module, default: module }));
  }
  return true;
}

module.exports = { classify, requestDetails, installNetworkPolicy };
