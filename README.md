# Sonnette

> ## ⚠️ AI SLOP — vibe coded for single use
>
> This repository was written with an AI assistant for exactly one house, one
> doorbell and one Home Assistant. It works there. It has never run anywhere else.
> **More fragile than a snowman in a sauna.** Read everything before you trust
> anything, and expect to change most of it for your own setup.

Everything about the front door doorbell (Ring, through ring-mqtt) lives in this
repository: the service that drives, stores and classifies the photos; what Home
Assistant does around it (notifications, dashboard); and what is needed to deploy
both.

```
Ring ──► ring-mqtt ──MQTT──► doorbell-ai (service/) ──MQTT──► Home Assistant (ha/)
              ▲                  │  photos, ledger, logs                 │
              └── PRESS ─────────┘  photos at /sonnette-photos/…       └─► phone, dashboard
```

ring-mqtt is **not** in this repository and does not need to be: it is the gateway
to Ring, and it stays as it is.

## Who does what

| | |
|---|---|
| `docker-compose.yml` | The Portainer stack for the service, deployable as is from this repository (see *Deploy*). Pulls the image that the GitHub action publishes. |
| `.github/workflows/image.yml` | On every push to `main` that touches `service/`: runs the tests, then builds the image and publishes it to `ghcr.io/<owner>/sonnette:<version>` and `:latest`. |
| `service/` | Container `doorbell-ai`. Presses the snapshot button after a motion or a ring (`driver.py`), writes every photo, classifies it with a local model (OpenAI-compatible API, image plus question), runs the visit state machine, publishes entities and events over MQTT, computes the day summary (`summary.py`), keeps the chosen photos (`archive.py`), purges after 180 days, serves the photos over HTTP (`webserve.py`). |
| `deploy/` | `deploy-service.sh` (redeploy through Portainer), `render.sh` (tokens), `deploy.env.example` (template of the values specific to one installation), and two one-off scripts kept for the record: `retire-legacy.py`, `inject-journal.py`. |

Everything specific to one installation (host names, paths, accounts, notification
targets) lives in `deploy/deploy.env`, ignored by git, and in the Portainer stack
variables. The repository contains none of it.

### The Home Assistant side is no longer here

`ha/packages/sonnette.yaml`, `ha/dashboards/sonnette.yaml` and `deploy/deploy-ha.sh`
left on 2026-09-22 for a private `homeassistant` repository that holds the whole
configuration of one installation. They were kept here as templates with
`__NOTIFY_PHONE__`-style tokens precisely because this repository is public; a private
one carries the resolved values and needs no rendering step, so no token can survive it
and reach Home Assistant as is.

They had already drifted: this repository carried an English-commented version,
committed on 2026-09-19 and never deployed, while production ran the French one — 237
diverging lines. Two homes for one file is how that happens. What stays here is the
service: code, image, stack.

Anyone rebuilding the Home Assistant side from this repository will find those two
files in the history, before that date.

The texts shown inside Home Assistant (dashboard labels, notifications) are in French
on purpose: that is the language of the house.

## Entities in Home Assistant (MQTT discovery, device "Porch")

| Entity | Role |
|---|---|
| `event.porch` | `visit_started`, `visit_ended` (with `photos` and `dings`), `packet_seen`, `ding` |
| `binary_sensor.porch_visit_in_progress` | a visit is in progress |
| `binary_sensor.porch_person`, `binary_sensor.porch_animal` | from the first sighting to the end of the visit |
| `image.porch_latest_photo` | latest non-empty photo |
| `sensor.porch_summary` | the day summary; the dashboard reads its attributes |
| `sensor.porch_day` | the same summary for the day chosen on the dashboard (`doorbell/porch/day/set`) |
| `sensor.porch_journal_personne`, `_animal`, `_carton`, `_sonnerie` | the journal: one entry per kept photo, state = time to the second, attribute `photo`. Retained, no availability. Their **history** in the recorder is the dashboard journal (Chronicle Card) |
| `binary_sensor.porch_ring_link` | ring-mqtt is alive: computed by the service on **every** heartbeat (`watch.py`), `off` after 16 min of silence |
| `binary_sensor.porch_model` | the service **can classify** (inference probe every 5 min); expires after 15 min of silence |

Command accepted by the service: `doorbell/porch/archive/set` (payload = the `photo`
field of an event); answer on `doorbell/porch/archive/result`.

## What the system never claims

- **To name someone.** No identity, no head count, no direction of travel.
  "Who rang" = times and photos.
- **"Parcel delivered".** `packet_seen` means "someone was seen carrying a box".
  A parcel left on the ground is in the camera's blind spot.

## Deploy

### Once

1. **Installation values**: copy `deploy/deploy.env.example` to `deploy/deploy.env`
   and fill it in. The scripts need `ssh $HOST`, `$PORTAINER` (Portainer API key) and
   `$HA` (Home Assistant token) in the environment.
2. **On the host**: create `$SERVICE_HOME/data/config.yaml` (template:
   `service/config.example.yaml`; it holds the Ring device identifiers) and
   `$PHOTOS_HOST_DIR`, a folder of its own under the HA `media` folder.
3. **Portainer**: *Stacks > Add stack > Repository*, URL of this repository, branch
   `main`, path `docker-compose.yml`, and the variables `SERVICE_HOME`,
   `PHOTOS_HOST_DIR`, `PHOTOS_HOST` (and `TZ` if needed). Enable *GitOps updates* by
   polling (5 min): a push to `main` redeploys the stack by itself (a webhook is
   impossible, Portainer cannot be reached from GitHub). Write the stack and endpoint
   numbers into `deploy.env`. The image comes from the GitHub container registry, so
   the package `sonnette` must be public (it inherits the repository's visibility) or
   the host must be logged in to `ghcr.io`. Portainer's update job pulls before it
   deploys: a `build:` in the compose file is not enough, the image has to exist in a
   registry.
4. **Home Assistant**, in `configuration.yaml`: `packages: !include_dir_named packages`
   under `homeassistant:`, the dashboard in YAML mode (`lovelace: dashboards:` with
   `filename: dashboards/sonnette.yaml`), and the recorder at 90 days for the journal
   (`recorder: purge_keep_days: 90`, excluding what is heavy). Install **Chronicle
   Card** through HACS (default store).
5. **HA front doors**: every reverse proxy that opens HA (Traefik on the local network,
   "tailscale serve" from outside) must mount the prefix `/sonnette-photos/` to
   `doorbell-ai:8099` and strip it before forwarding. The dashboard references the
   photos by that relative path; without this mount, no thumbnail loads.

### Then

**The service deploys itself**: a push to `main` that touches `service/` runs the
tests and publishes the image (about two minutes); Portainer polls `main` every 5
minutes and redeploys the stack on every new commit. The container is recreated only
if the compose file or the image change, and the compose file pins the image tag.
Hence the rule: **every change to the service comes with a version change**, in
`service/pyproject.toml` and in `docker-compose.yml`, in the same commit. If Portainer
polls before the image is published, its job fails once ("manifest unknown") and
succeeds at the next poll. This automatic update knows nothing about the event
window: push a version change when nobody is expected at the door.

```sh
deploy/deploy-service.sh     # without waiting for the poll: pull and redeploy, after checking for silence
```

`deploy-service.sh` requires a clean, pushed repository (Portainer deploys the remote)
and refuses to restart the container if a photo is less than 3 minutes old.

Changing version: `version` in `service/pyproject.toml` **and** `image:` in
`docker-compose.yml` (the script checks that they match).

## Tests

The service needs Python 3.13 or newer. On a machine without it, in a container:

```sh
docker run --rm -v "$PWD/service":/src:ro -w /work python:3.13-slim sh -c \
  'pip -q install paho-mqtt==2.1.0 "PyYAML>=6" "pytest>=8" && cp -r /src/. . && python -m pytest -q'
```

The test tape in `service/tests/tapes/` is a real MQTT recording, **anonymised**:
device identifiers replaced by `LOC`/`DEV`, images replaced by synthetic images (same
duplicates), RTSP secret and Wi-Fi name removed. `probe.jpg`, the classifier's probe
image, is synthetic too.

The GitHub action runs the same tests before publishing an image.

House method: **sabotage rather than proofread.** Every new guard is checked by
mutation (break it, a test must go red). The wiring `open visit -> extended burst` was
found that way, not by reading.

## Rules never to rediscover

- **One driver only.** With `active_mode: true`, the service is what presses
  `…/take_snapshot/command`, the only message it ever writes under `ring/#`
  (`driver.py`). No HA automation may press that button in parallel.
- **Burst interval >= 11 s**: ring-mqtt silently ignores a request less than 10 s
  after the previous one. `Config.load` refuses a lower value.
- **Never enable the doorbell's live video stream**, and never point a generic camera
  at the RTSP stream: that kills the motion alerts, Ring app included. Motion detection
  stays ON.
- **Never version or back up `ring-state.json`**: full access to the Ring account.
  Nor `config.yaml`, nor `deploy/deploy.env`.
- **The photo server has no authentication.** None of the paths leading to it may ever
  become reachable from the Internet, or the whole history of faces at the door becomes
  public. Never a Tailscale Funnel on it.
- **The dashboard references photos by a RELATIVE path**
  (`/sonnette-photos/YYYY-MM/x.jpg`): it names no host, so it follows whichever origin
  you arrived through. **Adding a front door without mounting that prefix breaks the
  thumbnails.** The only exception: the two "Photo" buttons of the journal, which open
  the image in a new tab and need an absolute URL (one per door, `HA_URL_LAN` and
  `HA_URL_REMOTE` in `deploy.env`).
- **The photos are in no HA backup** (`media` is outside `config`).
- **The JSONL logs** (`$SERVICE_HOME/data/events-*.jsonl`) are the only durable source.
  The HA recorder keeps 90 days for the dashboard journal; what the recorder does not
  have, `deploy/inject-journal.py` can replay from the JSONL files.
- **Never take `last_reported` / `last_updated` of an MQTT entity as a sign of life.**
  HA writes the state of an MQTT entity only when a value changed; a heartbeat with
  identical content leaves no trace. That was the cause of false "Doorbell silent"
  alerts.
- **Chronicle Card** skips the `unknown`/`unavailable` states **and** the transition
  out of them, and never shows the very first state of an entity: that is why the
  journal sensors are retained and have no availability.
- Timestamped backup before any change to a file on the host.
- No attribution lines in commit messages.

## Rolling back

| Level | Action |
|---|---|
| Service version | put the previous tag back in `image:` of `docker-compose.yml` (every published version stays in the registry), commit, push, `deploy-service.sh` |
| Capture driving | `active_mode: false` in `config.yaml`, restart `doorbell-ai`. Without a driver, the service only gets about one photo per visit |
| HA side | `git revert` in the homeassistant repository, then its `deploy/deploy.sh` |
