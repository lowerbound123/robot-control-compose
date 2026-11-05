// Shared by the njs startup renderer and Vite. Never imported by browser code.
function invalid() { throw Error('Invalid WebUI server API configuration') }

function parseConfig(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)
      || Object.keys(value).some(key => !['llm', 'endpoint', 'model', 'api_key', 'timeout_s'].includes(key))) invalid()
  // The pinned njs CLI doesn't support object spread. Validate keys before
  // copying so Object.assign cannot interpret an untrusted __proto__ key.
  value = Object.assign(value.llm === false ? { endpoint: '', model: '', api_key: '', timeout_s: 30 }
    : { llm: true }, value)
  if (typeof value.llm !== 'boolean'
      || !['endpoint', 'model', 'api_key'].every(key => typeof value[key] === 'string')
      || typeof value.timeout_s !== 'number' || !Number.isFinite(value.timeout_s)
      || value.timeout_s <= 0 || value.timeout_s > 120
      || /[\x00-\x1f\x7f]/.test(value.model)
      || value.api_key.length > 8192 || !/^[A-Za-z0-9._~+/=-]*$/.test(value.api_key)) invalid()
  let endpoint = value.endpoint
  let host = '', tls_name = ''
  if (endpoint) {
    // RFC3986 ASCII URI characters. No userinfo, fragment, whitespace or
    // backslashes; use percent-encoding for non-ASCII path/query characters.
    const match = /^(https?):\/\/(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?)(?::([0-9]+))?([/?][A-Za-z0-9\-._~%!$&'()*+,;=:@/?]*)?$/.exec(endpoint)
    if (!match || /%(?![0-9A-Fa-f]{2})/.test(endpoint)
        || (match[3] && !(Number(match[3]) >= 1 && Number(match[3]) <= 65535))) invalid()
    host = match[2] + (match[3] ? ':' + match[3] : '')
    tls_name = match[2].replace(/^\[|\]$/g, '')
    const path = match[4] || '/'
    endpoint = match[1] + '://' + host + (path[0] === '?' ? '/' : '') + path
  }
  return { llm: value.llm, endpoint, model: value.model, api_key: value.api_key, timeout_s: value.timeout_s,
    enabled: value.llm && !!endpoint && !!value.model.trim(), host, tls_name }
}

function publicConfig(config) {
  return { llm: config.llm, enabled: config.enabled, model: config.model, timeout_s: config.timeout_s }
}

function nginxQuote(text) {
  // Nginx's script compiler expands $ even after a configuration backslash.
  // Dynamic URI data must be percent-encoded before entering a quoted value.
  if (text.includes('$')) invalid()
  return '"' + text.replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"'
}

function nginxLocation(config) {
  const timeout = Math.ceil(config.timeout_s * 1000) + 'ms'
  const errors = [], redirects = []
  // 499 is Nginx's internal client-abort code and is forbidden in error_page.
  for (let status = 400; status <= 599; status++) if (status !== 499) errors.push(status)
  for (let status = 300; status <= 399; status++) redirects.push(status)
  // Nginx verifies at most one intermediate CA by default. Let's Encrypt's
  // current Root YE hierarchy presents leaf + three CA certificates, so the
  // default depth rejects a valid chain with "unable to get local issuer
  // certificate" (502). Keep a small margin above the current chain length.
  const proxy = config.enabled ? `
    set $ai_endpoint ${nginxQuote(config.endpoint.replace(/\$/g, '%24'))};
    proxy_pass $ai_endpoint;
    proxy_pass_request_headers off;
    proxy_set_header Host ${nginxQuote(config.host)};
    proxy_set_header Authorization ${nginxQuote(config.api_key ? 'Bearer ' + config.api_key : '')};
    proxy_set_header Content-Type application/json;
    proxy_set_header Accept application/json;
    proxy_set_header Connection "";
    proxy_ssl_server_name on;
    proxy_ssl_name ${nginxQuote(config.tls_name)};
    proxy_ssl_verify on;
    proxy_ssl_verify_depth 4;
    proxy_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;
    proxy_connect_timeout ${timeout};
    proxy_send_timeout ${timeout};
    proxy_read_timeout ${timeout};
    proxy_next_upstream off;
    proxy_buffering off;
    proxy_cache off;
    proxy_hide_header Set-Cookie;
    proxy_hide_header WWW-Authenticate;
    proxy_hide_header Location;
    proxy_intercept_errors on;` : '    return 503;'
  return `location = /ai/chat/completions {
    default_type application/json;
    add_header Cache-Control "no-store" always;
    error_page ${errors.join(' ')} /_webui_ai_error.json;
    error_page ${redirects.join(' ')} =502 /_webui_ai_error.json;
    if ($request_method != POST) { return 405; }
    if ($webui_ai_request_origin != $webui_origin) { return 403; }
    if ($http_content_type !~* "^application/json(;.*)?$") { return 415; }
${proxy}
}
location /ai/ { return 404; }
location = /_webui_ai_error.json {
    internal;
    alias /tmp/webui-ai-error.json;
    default_type application/json;
    add_header Cache-Control "no-store" always;
}
`
}

function ipv4(value) {
  return /^(0|[1-9][0-9]{0,2})(\.(0|[1-9][0-9]{0,2})){3}$/.test(value)
    && value.split('.').every(part => Number(part) <= 255)
}

// RC uses plain HTTP. Keep the service hostname dynamic for Docker DNS, but
// resolve host-gateway from /etc/hosts: nginx's resolver does not consult it.
function rcUpstream(value, hosts) {
  // The pinned njs CLI does not support default function parameters.
  if (value === undefined) value = 'http://robot-control:4000'
  if (hosts === undefined) hosts = ''
  const reject = () => { throw Error('Invalid WebUI RC upstream') }
  if (typeof value !== 'string') reject()
  const match = /^http:\/\/([A-Za-z0-9.-]+):([0-9]+)$/.exec(value)
  if (!match || match[0] !== value || !(Number(match[2]) >= 1 && Number(match[2]) <= 65535)) reject()
  let host = match[1].toLowerCase()
  if (host.length > 253 || !host.split('.').every(label =>
    /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(label))) reject()
  if (/^[0-9.]+$/.test(host) && !ipv4(host)) reject()
  if (host === 'host.docker.internal') {
    const rows = hosts.split(/\r?\n/).map(line => line.split('#')[0].trim().split(/\s+/))
    const entry = rows.find(row => ipv4(row[0]) && row.slice(1).some(alias => alias.toLowerCase() === host))
    if (!entry) reject()
    host = entry[0]
  }
  return host + ':' + Number(match[2])
}

export default { parseConfig, publicConfig, nginxLocation, rcUpstream }
