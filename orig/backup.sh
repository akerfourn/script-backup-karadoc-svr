#!/bin/bash

if [[ $EUID -ne 0 ]]; then
        echo "Error : This script must be run as root (ie sudo)"
        exit 1
fi

mount -L backup /media/backup
rsync -av /home/ /media/backup/home/ --delete
rsync -av /var/snap/nextcloud/common/nextcloud/data/ /media/backup/nextcloud/ --delete
rsync -av /home/kerfadmin/nextcloud/ /media/backup/config/ --delete
umount /media/backup
