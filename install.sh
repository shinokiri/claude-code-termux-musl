#!/data/data/com.termux/files/usr/bin/bash
set -eu
adapter_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$adapter_dir/tools/install-user.py" "$@"
