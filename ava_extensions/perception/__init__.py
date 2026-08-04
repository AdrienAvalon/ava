"""Perception continue d'Avalon — Ava cesse d'etre aveugle entre deux questions.

⚠ CE QUE CE PAQUET CHANGE. Jusqu'ici Ava etait purement REACTIVE : elle ne percevait
  rien tant qu'on ne lui parlait pas. pve-02 pouvait tomber, une pile s'epuiser, une
  personne rentrer — elle l'ignorait jusqu'a ce qu'on le lui demande. Or Avalon EMET en
  permanence : le control plane boucle sur 25 modules, dont `home_assistant` qui porte
  la presence des personnes, les temperatures par piece, le chauffage et l'energie.

⚠ LE MECANISME, ET POURQUOI IL EST SI SOBRE. Le WebSocket du CP sert de **cloche** :
  il ne porte qu'un `score_update` toutes les 5 minutes (mesure : 249 s d'attente pour
  le premier message). Ava s'en sert pour se reveiller, relit `/api/v1/dashboard`
  (37 Ko, 3,5 ms depuis la DMZ) et **compare elle-meme**. C'est exactement le motif
  deja en production dans `matrix_alerting` et `ai_analysis` du CP — on copie la
  doctrine maison plutot que d'en inventer une.

⚠ AUCUN CHANGEMENT D'INFRASTRUCTURE N'EST REQUIS. Le port 8100 est deja ouvert depuis
  la DMZ (« for avalon_status tool » dans `host_vars`), le CP est dual-home et lit
  Home Assistant a notre place. Ava n'acquiert aucun acces direct au LAN, et son
  module HA reste en **lecture seule** : meme compromise, elle ne peut rien commander.
"""

from ava_extensions.perception.qualification import (  # noqa: F401
    Changement,
    Niveau,
    a_dire,
    qualifier,
    qualifier_infra,
    qualifier_maison,
)
