#!/usr/bin/env bash
# Executed on the server through SSH, never directly on the local machine.
set -euo pipefail
umask 077

config_directory=$1
snap_directory=/var/snap/nextcloud/current/nextcloud

if [[ ! -f "$snap_directory/config/config.php" || ! -d "$snap_directory/extra-apps" ]]; then
    echo "Installation Nextcloud Snap introuvable." >&2
    exit 1
fi

if [[ -L "$config_directory" || -L "$config_directory/apps" ]]; then
    echo "Le répertoire de préparation et apps/ ne doivent pas être des liens symboliques." >&2
    exit 1
fi

mkdir -p -- "$config_directory"
temporary_directory=$(mktemp -d "$config_directory/.prepare.XXXXXXXX")
trap 'rm -rf -- "$temporary_directory"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP

nextcloud.mysqldump > "$temporary_directory/dump.sql"
if [[ ! -s "$temporary_directory/dump.sql" ]]; then
    echo "Le dump SQL est vide ; les exports précédents sont conservés." >&2
    exit 1
fi

cp -a -- "$snap_directory/config/config.php" "$temporary_directory/config.php"
mkdir -p -- "$config_directory/apps"
rsync -a --delete -- "$snap_directory/extra-apps/" "$config_directory/apps/"
mv -fT -- "$temporary_directory/dump.sql" "$config_directory/dump.sql"
mv -fT -- "$temporary_directory/config.php" "$config_directory/config.php"
