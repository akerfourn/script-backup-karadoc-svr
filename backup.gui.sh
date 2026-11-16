#!/usr/bin/env bash
set -euo pipefail

script_directory=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
python_command="$script_directory/.venv/bin/python"
if [[ ! -x "$python_command" ]]; then
    echo "Créer .venv et installer requirements-gui.txt (voir README.md)." >&2
    exit 1
fi
exec "$python_command" "$script_directory/backup_gui.py" "$@"
