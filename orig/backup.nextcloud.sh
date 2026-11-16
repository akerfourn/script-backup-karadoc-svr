cd nextcloud/
nextcloud.mysqldump > dump.sql
cp /var/snap/nextcloud/current/nextcloud/config/config.php config.php
rm -Rf apps/
cp -R /var/snap/nextcloud/current/nextcloud/extra-apps/ apps/
