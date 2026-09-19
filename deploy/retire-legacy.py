#!/usr/bin/env python3
"""Retire l'ancien systeme de sonnette de la configuration Home Assistant.

A executer DANS le conteneur homeassistant (plusieurs fichiers sont a root) :

    docker cp deploy/retire-legacy.py homeassistant:/tmp/ && \
    docker exec homeassistant python /tmp/retire-legacy.py [--dry-run]

Geste unique, fait le 2026-09-17 ; garde au depot pour memoire et parce qu'il
est rejouable sans danger : chaque etape est idempotente, et chaque fichier
modifie recoit d'abord une sauvegarde horodatee. Rien n'est supprime du disque :
les scripts et les copies de /config/www sont DEPLACES sous retire-2026-09-17/.

Ne touche PAS aux photos de /media/sonnette : leur suppression a ete un geste
separe, decide par l'utilisateur, apres copie de la photo gardee a la main.

Prealable non negociable (handover §4) : le service pilote deja les captures
(active_mode: true, prouve sur un vrai mouvement) et les six automatisations
ci-dessous sont desactivees. L'inverse ferait tomber l'entree du service de
six photos a une par visite.

Apres un exit 0 : check_config, puis recharger automatisations, scripts et
entites command_line. Les services shell_command.sonnette_* ne disparaissent
qu'au prochain redemarrage de HA ; ils n'ont plus aucun appelant.
"""
import re
import shutil
import sys
import time
from pathlib import Path

CONFIG = Path("/config")
STAMP = time.strftime("%Y-%m-%d-%H%M%S")
DRY = "--dry-run" in sys.argv

AUTOMATIONS = {"sonnette_evenement", "sonnette_photo", "sonnette_classer",
               "sonnette_archiver", "sonnette_supervision", "sonnette_purge"}
SCRIPTS = {"sonnette_purger_vides", "sonnette_archiver_sonnerie",
           "sonnette_archiver_mouvement", "sonnette_archiver_paquet"}


def save(path, text):
    if DRY:
        print(f"   [dry-run] {path} : {len(path.read_text())} -> {len(text)} octets")
        return
    shutil.copy2(path, f"{path}.bak-{STAMP}")
    path.write_text(text)


def blocks(text, start):
    """Decoupe en blocs dont la premiere ligne verifie `start`; le preambule
    eventuel est le bloc 0."""
    out = [[]]
    for line in text.splitlines(keepends=True):
        if start(line):
            out.append([])
        out[-1].append(line)
    return ["".join(b) for b in out]


def automations():
    path = CONFIG / "automations.yaml"
    parts = blocks(path.read_text(), lambda l: l.startswith("- id:"))
    kept, dropped = [], []
    for b in parts:
        m = re.match(r"- id:\s*['\"]?([^'\"\s]+)", b)
        (dropped if m and m.group(1) in AUTOMATIONS else kept).append(b)
    print(f"automations.yaml : {len(dropped)} retiree(s), {len(kept) - 1} conservee(s)")
    if dropped:
        save(path, "".join(kept))


def scripts():
    path = CONFIG / "scripts.yaml"
    parts = blocks(path.read_text(), lambda l: re.match(r"^[A-Za-z0-9_]+:", l))
    kept = [b for b in parts
            if not (re.match(r"^([A-Za-z0-9_]+):", b) or [None, ""])[1] in SCRIPTS]
    print(f"scripts.yaml : {len(parts) - len(kept)} retire(s)")
    if len(kept) != len(parts):
        save(path, "".join(kept))


def configuration():
    """Retire shell_command: (que des sonnette_*) et command_line: (le seul
    capteur sonnette_statistiques), avec les commentaires qui les precedent.
    Refuse d'agir si l'un des deux blocs porte autre chose."""
    path = CONFIG / "configuration.yaml"
    lines = path.read_text().splitlines(keepends=True)

    def top_block(key):
        try:
            i = next(n for n, l in enumerate(lines) if l.startswith(f"{key}:"))
        except StopIteration:
            return None
        j = i + 1
        while j < len(lines) and (lines[j].startswith((" ", "\t"))
                                  or not lines[j].strip()):
            j += 1
        while j > i and not lines[j - 1].strip():
            j -= 1
        k = i
        while k > 0 and lines[k - 1].startswith("#"):
            k -= 1
        return k, i, j

    removed = 0
    for key, must_all_match in (("shell_command", r"^\s+sonnette_\w+:"),
                                ("command_line", None)):
        span = top_block(key)
        if span is None:
            continue
        k, i, j = span
        body = lines[i + 1:j]
        if must_all_match:
            entries = [l for l in body if re.match(r"^  \w+:", l)]
            foreign = [l for l in entries if not re.match(must_all_match, l)]
        else:
            entries = [l for l in body if "unique_id:" in l]
            foreign = [l for l in entries if "sonnette_statistiques" not in l]
        if foreign or not entries:
            sys.exit(f"configuration.yaml : le bloc {key}: porte autre chose que la "
                     f"sonnette, je n'y touche pas : {foreign!r}")
        print(f"configuration.yaml : bloc {key}: retire (lignes {k + 1}-{j})")
        del lines[k:j]
        removed += 1
    if removed:
        save(path, "".join(lines))


def files():
    for folder, pattern in ((CONFIG / "scripts", "sonnette-*"),
                            (CONFIG / "www", "sonnette")):
        found = sorted(folder.glob(pattern))
        if not found:
            continue
        target = folder / "retire-2026-09-17"
        print(f"{folder} : {len(found)} element(s) -> {target.name}/")
        if DRY:
            continue
        target.mkdir(exist_ok=True)
        for f in found:
            shutil.move(str(f), str(target / f.name))


automations()
scripts()
configuration()
files()
print("dry-run : rien n'a ete ecrit" if DRY else "fait")
