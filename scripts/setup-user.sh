#!/bin/sh
# Run on the Docker host as the user who should own newly saved files.
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root"
rc_uid=$(id -u)
rc_gid=$(id -g)
if [ "$rc_uid" -eq 0 ]; then
    echo '请以日常使用项目的普通用户运行 setup-user.sh，不要使用 sudo。' >&2
    exit 1
fi
if [ -L .env ]; then
    echo '.env 是符号链接；请先将本机配置保存为项目内的普通文件。' >&2
    exit 1
fi

# Do not source .env: it is data and can contain credentials or shell syntax.
# Preserve the original content, including multiline quoted values. Compose
# takes the last assignment, so maintain only our exact three-line footer.
umask 077
temporary=$(mktemp .env.user.XXXXXX)
trap 'rm -f "$temporary"' EXIT HUP INT TERM
source=.env
if [ ! -f "$source" ]; then source=.env.example; fi
marker='# Host identity managed by scripts/setup-user.sh (last assignment wins)'
awk -v marker="$marker" '
    { lines[NR] = $0 }
    END {
        last = NR
        if (NR >= 3 && lines[NR-2] == marker &&
            lines[NR-1] ~ /^RC_UID=[0-9]+$/ && lines[NR] ~ /^RC_GID=[0-9]+$/)
            last -= 3
        for (i = 1; i <= last; i++) print lines[i]
        print marker
    }
' "$source" > "$temporary"
printf 'RC_UID=%s\nRC_GID=%s\n' "$rc_uid" "$rc_gid" >> "$temporary"

# Existing data is deliberately left alone: no recursive chown/chmod.
# Custom Qwen mount paths should be created by their owner separately.
mkdir -p outputs snapshot models cache/huggingface
mv "$temporary" .env
printf '已在 .env 设置 RC_UID=%s、RC_GID=%s，并准备默认挂载目录。\n' "$rc_uid" "$rc_gid"
