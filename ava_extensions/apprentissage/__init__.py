"""Boucle d'apprentissage — l'usage reel d'Ava devient sa feuille de route.

⚠ VOLONTAIREMENT MODESTE, et le dire fait partie du travail. L'intention etait un
  detecteur d'echecs ; la mesure du corpus l'a invalidee (zero formule d'echec sur
  51 traces, et un signal de « reformulation » domine par des tests en rafale).
  Ecrire un detecteur sur un corpus qui ne porte pas le signal reviendrait a inventer
  les motifs — et a obtenir un outil qui ne trouve jamais rien tout en paraissant
  fonctionner. Cf. le docstring de `rapport.py`.
"""

from ava_extensions.apprentissage.rapport import analyser, formuler  # noqa: F401
