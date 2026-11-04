// Runs once with the pinned Nginx image's njs CLI; no resident JS backend.
import fs from 'fs'
import configuration from './server-config.mjs'

try {
  const path = process.env.RC_WEBUI_CONFIG_PATH || '/run/robot-control-webui/config.json'
  const content = fs.readFileSync(path, 'utf8')
  if (content.length > 65536) throw Error('oversized config')
  const config = configuration.parseConfig(JSON.parse(content))
  fs.writeFileSync('/tmp/webui-public.json', JSON.stringify(configuration.publicConfig(config)), { mode: 0o600 })
  fs.writeFileSync('/tmp/webui-ai.conf', configuration.nginxLocation(config), { mode: 0o600 })
  fs.writeFileSync('/tmp/webui-ai-error.json', '{"error":"AI proxy request failed"}', { mode: 0o600 })
} catch (_) {
  // JSON/parser/fs exceptions may contain configuration values. Never print them.
  throw Error('Cannot load WebUI server config; check endpoint, model, api_key and timeout_s')
}
