#!/bin/sh
set -eu
# Bearer token syntax excludes nginx quote/directive interpolation characters.
# Neither the token nor the rendered configuration is printed.
case "${ROBOT_CONTROL_MCP_TOKEN:-}" in
    *[!A-Za-z0-9._~+/=-]*)
        echo 'Invalid ROBOT_CONTROL_MCP_TOKEN: expected a Bearer token' >&2
        exit 1
        ;;
esac
RC_MCP_AUTHORIZATION=""
if [ -n "${ROBOT_CONTROL_MCP_TOKEN:-}" ]; then
    RC_MCP_AUTHORIZATION="Bearer ${ROBOT_CONTROL_MCP_TOKEN}"
fi
export RC_MCP_AUTHORIZATION
umask 077
njs -m /etc/nginx/webui/render-config.mjs
envsubst '${RC_MCP_AUTHORIZATION}' < /etc/nginx/rc.conf.template > /tmp/rc-nginx.conf
exec nginx -c /tmp/rc-nginx.conf -g 'daemon off;'
