# Shadow d'autonomie infrastructure v3

Ce corpus synthetique peut etre execute dans trois modes explicitement distincts :

- `offline`, avec tout reseau interdit ;
- `loopback`, avec un backend HTTP local literal et sans credential fournisseur ;
- `configured-anthropic`, avec le `CloudEngine` de la release attestee et uniquement
  son `ANTHROPIC_API_KEY` ambiant parmi les credentials fournisseur connus.

Le dernier mode exige une attestation `adapter=cloud`, `provider=anthropic` et un
modele `claude-*`. Il est mutuellement exclusif de `--backend-url`. Les proxies et
les credentials des autres fournisseurs sont retires avant la construction du
moteur. Chaque cas reste un unique appel sans outil, memoire, trace, perception,
retry ni reparation. Une completion tronquee, vide, avec appel d'outil, attribuee a
un autre modele ou qui n'est pas un objet JSON strict annule la publication.

Un bundle vert ne prouve que le comportement du modele atteste sur ces situations
synthetiques. Il ne valide aucune execution reelle, ne devient pas une connaissance
canonique et ne constitue ni une promotion, ni une autorisation d'activation, ni une
autorisation d'agir sur Avalon. Une adjudication humaine et independante ainsi que
le chemin GitOps restent externes et obligatoires.
