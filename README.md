# Sonnette

Tout le système de la sonnette d'entrée (Ring, via ring-mqtt) tient dans ce dépôt :
le service qui pilote, écrit et classe les photos ; ce que Home Assistant fait autour
(notifications, tableau de bord) ; et de quoi déployer les deux.

```
Ring ──► ring-mqtt ──MQTT──► doorbell-ai (service/) ──MQTT──► Home Assistant (ha/)
              ▲                  │  photos, registre, journaux        │
              └── PRESS ─────────┘  photos par /sonnette-photos/…   └─► téléphone, tableau de bord
```

ring-mqtt n'est **pas** dans ce dépôt et n'a pas à y être : c'est la passerelle vers
Ring, elle reste telle quelle.

## Qui fait quoi

| | |
|---|---|
| `docker-compose.yml` | La pile Portainer du service, déployable telle quelle depuis ce dépôt (voir *Déployer*). Construit l'image depuis `service/`. |
| `service/` | Conteneur `doorbell-ai`. Presse le bouton d'instantané après un mouvement ou une sonnerie (`driver.py`), écrit chaque photo, la classe par un modèle local (API compatible OpenAI, image + question), tient la machine à états des visites, publie entités et événements par MQTT, calcule le résumé du jour (`summary.py`), garde les photos choisies (`archive.py`), purge à 180 jours, sert les photos en HTTP (`webserve.py`). |
| `ha/packages/sonnette.yaml` | Ce que seul HA peut faire : les avis sur le téléphone (sonnerie, colis sans sonnerie, confirmation « Gardée », deux supervisions) et les réglages (`input_number`, jour affiché). Aucun script shell, aucun fichier écrit par HA. |
| `ha/dashboards/sonnette.yaml` | Le tableau de bord. Répond à trois questions : ma livraison est-elle arrivée, qui a sonné aujourd'hui, y a-t-il eu du mouvement cette nuit. Le journal est une carte tierce, Chronicle Card (HACS), qui lit l'historique des `sensor.porch_journal_*`. |
| `deploy/` | `deploy-service.sh` (redéploiement par Portainer), `deploy-ha.sh` (package et tableau), `render.sh` (jetons), `deploy.env.example` (modèle des valeurs propres à une installation), et deux gestes uniques gardés pour mémoire : `retire-legacy.py`, `inject-journal.py`. |

Tout ce qui est propre à une installation (noms d'hôte, chemins, comptes, cibles de
notification) vit dans `deploy/deploy.env`, ignoré par git, et dans les variables de la
pile Portainer. Le dépôt n'en contient aucun.

## Entités dans Home Assistant (découverte MQTT, dispositif « Porch »)

| Entité | Rôle |
|---|---|
| `event.porch` | `visit_started`, `visit_ended` (avec `photos` et `dings`), `packet_seen`, `ding` |
| `binary_sensor.porch_visit_in_progress` | une visite est en cours |
| `binary_sensor.porch_person`, `binary_sensor.porch_animal` | du premier aperçu à la fin de la visite |
| `image.porch_latest_photo` | dernière photo non vide |
| `sensor.porch_summary` | le résumé du jour ; le tableau de bord lit ses attributs |
| `sensor.porch_day` | le même résumé pour le jour choisi sur le tableau (`doorbell/porch/day/set`) |
| `sensor.porch_journal_personne`, `_animal`, `_carton`, `_sonnerie` | le journal : une entrée par photo retenue, état = heure à la seconde, attribut `photo`. Retenus, sans availability. Leur **historique** dans le recorder est le journal du tableau (Chronicle Card) |
| `binary_sensor.porch_ring_link` | ring-mqtt est vivant : calculé par le service sur **chaque** battement (`watch.py`), `off` après 16 min de silence |
| `binary_sensor.porch_model` | le service **sait classer** (sonde d'inférence toutes les 5 min) ; expire après 15 min de silence |

Commande acceptée par le service : `doorbell/porch/archive/set` (charge = le champ
`photo` d'un événement) ; réponse sur `doorbell/porch/archive/result`.

## Ce que le système ne prétend jamais

- **Nommer quelqu'un.** Ni identité, ni nombre de personnes, ni sens de passage.
  « Qui a sonné » = des heures et des photos.
- **« Colis livré ».** `packet_seen` veut dire « quelqu'un a été vu portant un carton ».
  Un colis posé au sol est dans l'angle mort de la caméra.

## Déployer

### Une seule fois

1. **Valeurs de l'installation** : copier `deploy/deploy.env.example` en
   `deploy/deploy.env` et le remplir. Les scripts exigent `ssh $HOST`, `$PORTAINER`
   (clé d'API Portainer) et `$HA` (jeton Home Assistant) dans l'environnement.
2. **Sur l'hôte** : créer `$SERVICE_HOME/data/config.yaml` (modèle :
   `service/config.example.yaml`, il porte les identifiants de l'appareil Ring) et
   `$PHOTOS_HOST_DIR`, un sous-dossier à lui sous le dossier `media` de HA.
3. **Portainer** : *Stacks > Add stack > Repository*, URL de ce dépôt, branche `main`,
   chemin `docker-compose.yml`, et les variables `SERVICE_HOME`, `PHOTOS_HOST_DIR`,
   `PHOTOS_HOST` (et `TZ` au besoin). Portainer construit l'image depuis `service/`.
   Activer *GitOps updates* par scrutation (5 min) : un push sur `main` redéploie la
   pile tout seul (un webhook est impossible, Portainer n'est pas joignable depuis
   GitHub). Reporter dans `deploy.env` le numéro de la pile et de l'endpoint.
4. **Home Assistant**, dans `configuration.yaml` : `packages: !include_dir_named packages`
   sous `homeassistant:`, le tableau en mode YAML (`lovelace: dashboards:` avec
   `filename: dashboards/sonnette.yaml`), et le recorder à 90 jours pour le journal
   (`recorder: purge_keep_days: 90`, en excluant ce qui pèse). Installer **Chronicle
   Card** par HACS (magasin par défaut).
5. **Portes d'entrée de HA** : chaque reverse proxy par lequel on ouvre HA (Traefik sur
   le réseau local, « tailscale serve » depuis l'extérieur) doit monter le préfixe
   `/sonnette-photos/` vers `doorbell-ai:8099` et le retirer avant de transmettre.
   Le tableau référence les photos par ce chemin relatif ; sans ce montage, aucune
   vignette ne charge.

### Ensuite

**Le service se déploie tout seul** : Portainer scrute `main` toutes les 5 minutes et
redéploie la pile à chaque nouveau commit. Le conteneur n'est recréé que si le compose
ou l'image changent ; l'image n'est reconstruite que si son étiquette est nouvelle, d'où
la règle : **tout changement du service s'accompagne d'un changement de version**. Cette
mise à jour automatique ne connaît pas la fenêtre d'événement : pousser un changement de
version quand personne n'est attendu à la porte.

```sh
deploy/deploy-service.sh     # sans attendre la scrutation : pull and redeploy, apres verification du silence
deploy/deploy-ha.sh          # rend les jetons, copie package + tableau, sauvegardes horodatees, check_config
```

`deploy-service.sh` exige un dépôt propre et poussé (Portainer déploie le remote) et
refuse de redémarrer le conteneur si une photo a moins de 3 minutes. `deploy-ha.sh` ne recharge rien ; après un exit 0,
recharger « Toute la configuration YAML » dans HA. Un changement du recorder demande
un redémarrage de HA.

Changer de version : `version` dans `service/pyproject.toml` **et** `image:` dans
`docker-compose.yml` (le script vérifie qu'ils concordent).

## Tests

Le service exige Python ≥ 3.13. Sur une machine qui ne l'a pas, dans un conteneur :

```sh
docker run --rm -v "$PWD/service":/src:ro -w /work python:3.13-slim sh -c \
  'pip -q install paho-mqtt==2.1.0 "PyYAML>=6" "pytest>=8" && cp -r /src/. . && python -m pytest -q'
```

La bande d'essai `service/tests/tapes/` est un enregistrement MQTT réel, **anonymisé** :
identifiants de l'appareil remplacés par `LOC`/`DEV`, images remplacées par des images
synthétiques (mêmes doublons), secret RTSP et nom du Wi-Fi retirés. `probe.jpg`, l'image
de sonde du classificateur, est synthétique elle aussi.

Méthode de la maison : **faire saboter plutôt que relire.** Toute garde nouvelle est
vérifiée par mutation (on la casse, un test doit rougir). Le câblage
`visite ouverte → rafale prolongée` a été trouvé ainsi, pas à la lecture.

## Règles à ne jamais redécouvrir

- **Un seul conducteur.** Avec `active_mode: true`, c'est le service qui presse
  `…/take_snapshot/command` — le seul message qu'il écrive jamais sous `ring/#`
  (`driver.py`). Aucune automatisation HA ne doit presser ce bouton en parallèle.
- **Intervalle de rafale ≥ 11 s** : ring-mqtt ignore en silence une demande à moins de
  10 s de la précédente. `Config.load` refuse une valeur plus basse.
- **Ne jamais activer le flux vidéo en direct de la sonnette**, ni pointer une caméra
  générique sur le flux RTSP : cela supprime les alertes de mouvement, application Ring
  comprise. La détection de mouvement reste ON.
- **Ne jamais versionner ni sauvegarder `ring-state.json`** : accès complet au compte
  Ring. Ni `config.yaml`, ni `deploy/deploy.env`.
- **Le serveur de photos n'a aucune authentification.** Aucun des chemins qui y mènent
  ne doit jamais devenir routable depuis Internet — sinon tout l'historique des
  visages à la porte devient public. Jamais de Funnel Tailscale dessus.
- **Le tableau de bord référence les photos par un chemin RELATIF**
  (`/sonnette-photos/AAAA-MM/x.jpg`) : il ne nomme aucun hôte, et suit donc
  l'origine par laquelle on est arrivé. **Ajouter une porte d'entrée sans y monter ce
  préfixe casse les vignettes.** Seule exception : les deux boutons « Photo » du journal,
  qui ouvrent l'image dans un nouvel onglet et exigent une URL absolue (une par porte,
  `HA_URL_LAN` et `HA_URL_REMOTE` dans `deploy.env`).
- **Les photos ne sont dans aucune sauvegarde de HA** (`media` est hors de `config`).
- **Les journaux JSONL** (`$SERVICE_HOME/data/events-*.jsonl`) sont la seule source
  durable. Le recorder de HA garde 90 jours pour le journal du tableau ; ce que le
  recorder ne garde pas, `deploy/inject-journal.py` sait le rejouer depuis les JSONL.
- **Ne jamais prendre `last_reported` / `last_updated` d'une entité MQTT pour un signe de
  vie.** HA n'écrit l'état d'une entité MQTT que si une valeur a changé ; un battement au
  contenu identique ne laisse aucune trace. C'est la cause de fausses alertes
  « Sonnette muette ».
- **Chronicle Card** saute les états `unknown`/`unavailable` **et** la transition qui en
  sort, et n'affiche jamais le tout premier état d'une entité : c'est pourquoi les
  capteurs du journal sont retenus et sans availability.
- Sauvegarde horodatée avant toute modification d'un fichier sur l'hôte.
- Aucune ligne d'attribution dans les messages de commit.

## Retour arrière

| Niveau | Geste |
|---|---|
| Version du service | remettre l'`image:` précédente dans `docker-compose.yml`, commiter, pousser, `deploy-service.sh` |
| Pilotage des captures | `active_mode: false` dans `config.yaml`, redémarrer `doorbell-ai`. Sans conducteur, le service ne reçoit plus qu'environ une photo par visite |
| Côté HA | les sauvegardes `*.bak-<horodatage>` que `deploy-ha.sh` laisse à côté de chaque fichier remplacé |
