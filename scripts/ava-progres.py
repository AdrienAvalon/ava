#!/usr/bin/env python3
"""Rapport d'usage d'Ava — ce qu'elle fait, et ce que cela designe comme travail.

⚠ A LANCER SUR LA VM, la ou vivent les traces :
    ssh avalon@192.168.2.40 "ssh avalon@192.168.100.15 \\
      'cd /home/avalon/ava && ./.venv/bin/python scripts/ava-progres.py'"

⚠ Ce rapport DIT quand il n'a pas de quoi conclure. C'est son principal interet : un
  rapport qui affirmerait « aucun outil ne manque » sur douze echanges serait faux, et
  on le croirait — exactement le defaut que ce projet documente partout ailleurs.
"""

from __future__ import annotations

import sys

from ava_extensions.apprentissage import analyser, formuler


def main() -> int:
    jours = float(sys.argv[1]) if len(sys.argv) > 1 else 7.0
    try:
        from openjarvis.core.config import load_config

        actifs = [
            x.strip() for x in (load_config().agent.tools or "").split(",") if x.strip()
        ]
    except Exception:  # noqa: BLE001
        actifs = []
    print(f"── Ava, {jours:.0f} derniers jours ──")
    print(formuler(analyser(depuis_secondes=jours * 86400), outils_actifs=actifs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
