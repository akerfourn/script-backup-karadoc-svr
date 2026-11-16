# Miroir distant avec rsync

Un outil séparé permet de
[tester le HGST par zones avec reprise](#test-du-hgst-avec-reprise).

Ces scripts se lancent à Rouen, avec le disque de sauvegarde déjà monté.
Ils récupèrent les données du serveur à Ermont par SSH et conservent
l'organisation des sauvegardes historiques :

| Source sur le serveur | Destination sur le disque |
| --- | --- |
| `/home/` | `home/` |
| `/var/snap/nextcloud/common/nextcloud/data/` | `nextcloud/` |
| `/home/kerfadmin/nextcloud/` | `config/` |

Les fichiers supprimés du serveur sont supprimés du miroir. Les autres
répertoires à la racine du disque restent inchangés. Les scripts originaux
sont conservés dans `orig/`.

## Interface graphique

L'interface PySide6 demande le mot de passe du serveur une fois, puis le
réutilise pendant la sauvegarde. Une connexion SSH est partagée entre les
commandes. Les champs restent en mémoire pour pouvoir simuler puis lancer
le miroir ; ils sont effacés à la fermeture de la fenêtre.

Installation locale, sans sudo :

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-gui.txt
./backup.gui.sh
```

La seule dépendance facultative est `PySide6-Essentials` : elle fournit les
widgets Qt, sans les modules WebEngine et autres extensions inutilisées.
Le moteur rsync et la ligne de commande restent indépendants de Qt.

Dans la fenêtre :

1. Renseigner le serveur et le point de montage du disque.
2. Saisir le mot de passe SSH. Le champ peut rester vide si une clé chargée
   dans l'agent permet déjà la connexion.
3. Si le mot de passe sudo distant est différent, décocher la case
   correspondante et le saisir séparément. Pour une connexion par clé,
   utiliser ce champ séparé si sudo demande un mot de passe.
4. Cliquer sur **Simuler**, puis sur **Synchroniser le miroir**.

L'application demande séparément le mot de passe administrateur du PC si
sudo n'a pas déjà une autorisation valide. Seul le processus de copie est
élevé ; ne pas lancer l'interface avec sudo.

Les mots de passe passent par un socket Unix privé, accessible uniquement
au compte local et à root. Ils ne sont écrits ni dans un fichier, ni dans
les arguments des commandes, ni dans les variables d'environnement.
L'authentification sudo distant est terminée avant de transmettre les
données rsync. Aucune modification de sudoers ou installation sur le serveur
n'est nécessaire.

Le transport gère les tubes non bloquants : si rsync ralentit la lecture,
il attend que la sortie soit disponible et transmet tous les octets, même
après une écriture partielle. Une saturation temporaire du tube ne doit
pas être signalée comme une erreur d'authentification.

Le bouton **Arrêter** ferme la connexion SSH et interrompt le transfert.
L'interface attend la fin de rsync et de son nettoyage avant d'afficher
**Interrompu**. Elle empêche aussi le démarrage de la phase suivante si
l'arrêt arrive juste entre deux transferts.

Pour reprendre le lendemain :

1. Cliquer sur **Arrêter** et attendre le message **Interrompu**.
2. Éjecter ou démonter normalement le disque avant de le débrancher.
3. Le lendemain, remonter le même disque et relancer la synchronisation
   avec les mêmes paramètres.

Les fichiers déjà copiés et inchangés sont ignorés. Le fichier incomplet
est conservé dans `.rsync-partial`, séparément de l'ancienne version, et sert
de base à la reprise. Rsync doit néanmoins refaire son parcours et peut
relire des données : la reprise n'est pas forcément instantanée.
Le miroir reste partiellement actualisé jusqu'à une synchronisation
complète, et la reprise tient compte de l'état actuel du serveur.

## Prérequis

- Machine locale Linux avec Python 3.10+, Bash, rsync et client OpenSSH.
- Serveur avec Bash, rsync et SSH.
- Disque monté manuellement, sur un système de fichiers permettant de
  conserver les propriétaires et permissions Unix, comme auparavant.
- La synchronisation crée les répertoires manquants (`home/`, `nextcloud/`
  et `config/`) à la racine du disque monté. Pour la version allégée, seul
  `home/` est créé. Une simulation ne crée aucun répertoire.
- Connexion SSH fonctionnelle, avec la clé du serveur déjà vérifiée.
- Le compte distant doit pouvoir lire les sources, ou disposer de sudo.
  L'interface gère son mot de passe. La ligne de commande utilise par défaut
  une clé SSH et sudo **sans interaction** ; elle échoue si un mot de passe
  est nécessaire. Les scripts ne modifient aucune règle sudo.

Un alias dans `~/.ssh/config` permet de définir le serveur, le port et la clé.
Les exemples utilisent `ermont` et `/media/backup` : remplacer ces valeurs
par l'alias SSH et le point de montage réels.

Vérifier d'abord la connexion avec son compte habituel :

```bash
ssh ermont
```

Pour le tout premier accès, cette commande permet aussi de vérifier
l'empreinte du serveur. L'interface refuse une clé d'hôte inconnue ou changée.

Les transferts se lancent avec `sudo` pour conserver propriétaires et
permissions. Le transport SSH utilise automatiquement le compte ayant
lancé sudo, donc son `~/.ssh/config`, ses clés et ses hôtes connus.
`--preserve-env=SSH_AUTH_SOCK` permet aussi de conserver l'agent SSH.

Si le compte distant dispose déjà de tous les accès nécessaires, désactiver
sudo distant avec `--no-remote-sudo`. C'est également l'option à utiliser
avec une connexion SSH directe en tant que root, si elle est déjà configurée.
`--ssh-as utilisateur` sélectionne explicitement le compte SSH **local**
quand le script est exécuté directement en tant que root.

## Alias SSH

Créer ou modifier `~/.ssh/config` avec son compte habituel, sans sudo,
et ajouter ce bloc avant un éventuel `Host *` :

```sshconfig
Host karadoc
  HostName karadoc.kerfourn.net
  User kerfadmin
  Port 22
```

`ssh karadoc` devient alors l'équivalent de
`ssh kerfadmin@karadoc.kerfourn.net`. L'alias fonctionne aussi dans
l'interface et dans les scripts. Un alias mémorise les paramètres de
connexion ; il ne change pas le mode d'authentification.
Voir aussi la [documentation OpenSSH](https://man.openbsd.org/ssh_config).

Si le fichier n'existe pas encore :

```bash
mkdir -p ~/.ssh
chmod 700 ~/.ssh
touch ~/.ssh/config
chmod 600 ~/.ssh/config
```

Pour autoriser les mots de passe SSH dans une exécution en terminal,
ajouter `--interactive-ssh`. Cette option ne gère pas le mot de passe sudo
distant ; l'interface le prend en charge.

## Sauvegarde complète

Commencer par une simulation, qui ne modifie ni le miroir ni les exports
Nextcloud :

```bash
sudo --preserve-env=SSH_AUTH_SOCK \
  ./backup.sh ermont /media/backup --dry-run
```

Puis lancer le miroir, avec rafraîchissement préalable du dump SQL,
de `config.php` et des applications Nextcloud sur le serveur :

```bash
sudo --preserve-env=SSH_AUTH_SOCK \
  ./backup.sh ermont /media/backup --prepare-nextcloud
```

Sans `--prepare-nextcloud`, le script copie les exports déjà présents,
comme le script historique. Il faut donc avoir conscience de leur date.
La préparation requiert l'installation Nextcloud Snap et sa commande
`nextcloud.mysqldump`.

## Sauvegarde allégée

Copier uniquement `/home/` vers `home/` :

```bash
sudo --preserve-env=SSH_AUTH_SOCK \
  ./backup.small.sh ermont /media/backup --dry-run

sudo --preserve-env=SSH_AUTH_SOCK \
  ./backup.small.sh ermont /media/backup
```

## Préparation Nextcloud seule

L'équivalent distant de l'ancien `backup.nextcloud.sh` :

```bash
./backup.nextcloud.sh ermont
```

Cela rafraîchit uniquement les exports sur le serveur, sans toucher au
disque local. Le dump est écrit dans un fichier temporaire et ne remplace
l'ancien qu'après sa génération réussie.

## Chemins sources personnalisés

Après un futur déplacement des données, utiliser `--data-dir` sans changer
l'organisation du disque :

```bash
sudo --preserve-env=SSH_AUTH_SOCK \
  ./backup.sh ermont /media/backup \
  --data-dir /mnt/storage/cloud-data \
  --config-dir /home/kerfadmin/nextcloud \
  --dry-run
```

`--home-dir` permet également de changer la source de `home/`.
Tous ces chemins sont des chemins absolus **sur le serveur**.

## Comportement et limites

- Aucun montage ou démontage automatique : le disque reste monté à la fin.
- Refus si la destination n'est pas un point de montage, si elle vaut `/`,
  ou si un répertoire cible manque ou est un lien symbolique.
- Vérification de toutes les sources avant le premier transfert. Une source
  absente, illisible ou entièrement vide interrompt la copie.
- Arrêt à la première erreur SSH, sudo, dump ou rsync, avec un code non nul.
  Un transfert interrompu peut avoir déjà modifié une partie du miroir.
- Même mode archive que les scripts d'origine (`rsync -a`), avec conservation
  des UID/GID numériques du serveur (`--numeric-ids`), sans remappage selon
  les comptes locaux. Les suppressions sont appliquées à la fin de chaque
  transfert (`--delete-delay`).
- En simulation, les suppressions prévues sont listées avec
  `--delete-during`, pour éviter le fichier temporaire que `--delete-delay`
  peut créer même avec `--dry-run`. Aucun débit de transfert n'est affiché,
  puisqu'aucun contenu n'est transféré. Une copie réelle est refusée si le
  disque est monté en lecture seule.
- Les fichiers partiels restent dans `.rsync-partial` afin de faciliter la
  reprise. Relancer la même commande après une interruption ; rsync compare
  les fichiers existants et évite de tout retransférer.
- Ni compression réseau ni vérification systématique avec `--checksum` :
  rsync utilise sa comparaison habituelle par taille et date de modification,
  pour limiter les lectures sur le disque USB2.
- Ne pas lancer plusieurs sauvegardes ou préparations simultanément.

Comme les scripts historiques, cette version copie les fichiers à chaud.
Elle ne met pas Nextcloud en maintenance et ne garantit pas un état
transactionnel entre la base et les fichiers s'ils changent pendant la copie.
Pour cette première synchronisation, éviter les écritures pendant le transfert.

## Validation locale

```bash
python3 -m unittest discover -s tests -v
```

Les tests utilisent le vrai rsync avec un double local de SSH. Ils n'accèdent
ni au réseau, ni au serveur, ni à un disque réel. Ils vérifient notamment la
structure, les suppressions, les fichiers cachés, la simulation, les chemins
avec espaces, la version allégée et les refus avant transfert.

Les tests d'authentification vérifient aussi les permissions du socket,
la séparation du mot de passe sudo et du protocole binaire rsync, ainsi
que l'absence de secrets dans les arguments et l'environnement. Pour tester
également l'interface sans écran :

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest discover -s tests -v
```

Les tests du transport couvrent aussi les tubes saturés, les écritures
partielles, une entrée temporairement vide, une fermeture du lecteur et
un échange binaire de 4 Mio avec les flux standards non bloquants.

## Test du HGST avec reprise

Cet outil est indépendant de la sauvegarde. Il est réservé au disque
**HGST HTS721010A9E630 de 1 To** identifié pendant le diagnostic.
Il **efface les données et la table des partitions** à mesure qu'il avance.
Ne pas l'utiliser sur le disque Seagate contenant encore des fichiers.

Prérequis : Linux, Python 3.10+, `smartctl`, `lsblk` et `badblocks`
(paquets Ubuntu `smartmontools`, `util-linux` et `e2fsprogs`).
`badblocks` fournit l'écriture, la relecture et la comparaison, y compris
le relevé des erreurs ; Python gère les zones et la reprise.

Vérifier le nom du disque après chaque branchement, puis simuler :

```bash
lsblk -d -o NAME,SIZE,MODEL,TRAN
sudo ./surface-test.sh /dev/sdc --dry-run
```

Toutes ses partitions doivent être démontées et aucun autotest SMART ne
doit être en cours. Le script refuse une partition, un disque monté,
utilisé comme swap ou associé à un périphérique RAID/LVM/chiffré actif.
Le modèle, la capacité et une empreinte du numéro de série ATA sont
vérifiés avant et après chaque zone. Le numéro de série n'est pas journalisé.

Lancer une session, ici avec un budget d'environ une heure :

```bash
sudo ./surface-test.sh /dev/sdc --max-minutes 60
```

Chaque zone de **256 Mio** est écrite avec des zéros, puis relue et comparée.
Il n'y a qu'un motif, contrairement aux quatre motifs par défaut de
`badblocks -w`. Une zone entièrement parcourue est enregistrée, avec ses
éventuelles erreurs : « parcouru » ne signifie donc pas « sain ».
Les zones avec erreurs sont conservées dans le bilan et ne sont pas
automatiquement recommencées.

`--max-minutes` est une limite souple : le script termine la zone en cours
avant de s'arrêter, ce qui peut dépasser le délai sur une zone défectueuse.
Sans cette option, il continue jusqu'à la fin ou jusqu'à un arrêt manuel.
`--max-zones 10` limite plutôt le nombre de zones traitées pendant la session.

Pour arrêter manuellement, faire **Ctrl+C**, puis attendre **Arrêt terminé**
avant de débrancher le disque. Une E/S bloquée dans le noyau peut retarder
l'arrêt. La zone interrompue sera entièrement recommencée ; les zones déjà
enregistrées seront sautées. Le lendemain, relancer simplement :

```bash
sudo ./surface-test.sh /dev/sdc --max-minutes 60
```

Le nom `/dev/sdc` peut changer : utiliser le nom actuel du même disque.
L'empreinte enregistrée empêche de reprendre le journal sur un autre disque
du même modèle. Ne pas monter ou remplacer le disque pendant une session.

L'état et les journaux restent **sur le PC**, dans `.surface-test/` à côté
du script, quel que soit le répertoire depuis lequel il est lancé :

- `state.json` : point de reprise faisant autorité, remplacé atomiquement
  et synchronisé après chaque zone terminée ;
- `journal.jsonl` : sessions, zones, erreurs et compteurs SMART
  5/196/197/198/199 ;
- `zone-*.log` et `zone-*.badblocks` : sortie et blocs défectueux de chaque
  tentative, y compris les tentatives interrompues. Les numéros sont en
  **blocs de 4096 octets**, soit huit secteurs logiques de 512 octets.

Consulter l'avancement enregistré sans accéder au disque :

```bash
sudo ./surface-test.sh --status
```

Conserver ce dossier pour reprendre. `--state-dir /chemin/sur/le/PC` permet
de choisir son emplacement, à réutiliser à chaque lancement. `--zone-mib 64`
permet de choisir des zones plus petites au premier lancement ; leur taille
reste fixe ensuite. Les journaux sont ignorés par Git.

Codes de sortie : `0` session terminée, `2` blocs défectueux signalés dans
le bilan cumulé, `130` arrêt demandé, `1` erreur empêchant de continuer.
Une panne pendant une zone la laisse à refaire. Les tests couvrent cette
reprise et les limites des zones avec un vrai `badblocks` sur un petit
fichier temporaire, sans accès aux disques physiques.

Une écriture suivie d'une relecture ne suffit pas à qualifier ce disque
pour une sauvegarde fiable, même si les secteurs en attente disparaissent.
Voir le
[manuel de badblocks](https://man7.org/linux/man-pages/man8/badblocks.8.html).
