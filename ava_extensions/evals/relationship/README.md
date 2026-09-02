# Banc relationnel Ava

Ce module mesure hors ligne l'effet causal du garde relationnel sur 49 scénarios français
entièrement synthétiques. Il ne lit aucune conversation réelle, mémoire personnelle ou donnée de
production. Les sorties de modèle restent des candidats privés : aucun rapport, score, consensus de
reviewers ou ancrage ne devient une connaissance canonique.

## Contrat v3 gelé

`data/manifest.v3.json` est le point d'entrée courant. Il épingle le corpus 1.7.0, la rubrique
sémantique v2 et la politique de sûreté 1.7.1
`sha256:b6b4202ce7c4e227080fa4095d08c40b6d54ad8a7b57728de57600e3520d15a7`.
Le corpus contient 49 cas, dont 46 ont un principal vérifié, lié et opt-in. Les trois autres passent
par la persona commune : B appelle encore `prepare(None)` pour prouver leur couverture, mais ne doit
créer aucun objet garde ni appeler `begin`.

Les quatorze gates de sûreté sont binaires, ordonnés et non compensables. Les mesures lexicales de
chaleur, esprit, continuité et exactitude restent diagnostiques. La qualité promouvable exige les
deux adjudications exhaustives de la rubrique ; une abstention échoue. Le corpus et la rubrique
déclarent leur méthode de rédaction assistée, leur exposition éventuelle aux seuls diagnostics du
prédécesseur, l'absence d'accès aux nouvelles sorties candidates, l'absence de revue du propriétaire
et l'absence de données personnelles.

Les artefacts v1 et v2 sous `data/` restent historiques et lisibles bit pour bit. Leur manifeste ne
peut ni accepter un bundle v3 ni rendre un rapport promouvable. Le marqueur de déploiement reste
`.ava-release` au format `ava-release-v1` : l'attestation de release v2 est un document externe, pas
une nouvelle version de ce marqueur. Les consommateurs dead-man, relais, sauvegarde et rollback
continuent donc à lire leur contrat v1 inchangé.

## Paire causale A/B

Les deux releases proviennent de commits Git précis :

- A porte uniquement `RELATIONSHIP_GUARD_TREATMENT = "shadow-baseline-only-v1"` et reste
  `prepared_noncurrent` ; elle est préparée sans jamais devenir `ava-current` ;
- B est l'enfant direct et à parent unique de A, remplace exactement ce littéral par
  `"runtime-enforced-v1"`, puis devient `active_current` ;
- le chemin canonique est
  `ava_extensions/identity/relationship_guard_treatment.py`, mode Git `100644` ; aucun autre chemin,
  contenu ou mode ne peut différer.

Chaque release conserve son SHA brut de `source-tree.tar`, `rust-tree.tar`, wheel et attestation
Rust. Les cartes canoniques des archives tar sont recalculées après rejet des chemins ambigus,
doublons, liens, types spéciaux et tailles hors borne. Les tar Rust bruts peuvent différer à cause de
métadonnées déterminées par le commit ; leurs cartes canoniques doivent être identiques. De même,
les wheels brutes peuvent différer par leur conteneur ZIP, mais leur digest de payload canonique doit
être identique. La canonicalisation ZIP trie chemins, modes, tailles et contenus, inclut `RECORD`, et
rejette doublons, traversals, chiffrement, liens/types spéciaux, CRC invalide et bombes de
décompression. Le digest de recette est recalculé depuis les champs toolchain normalisés, le chemin
fixe et le SHA des octets exacts de `deploy/docker/Dockerfile.rust-builder` lus dans chaque archive
source ; il doit être égal entre A et B. L'ID brut de l'image builder reste attesté pour chaque
release mais peut différer, car son label de révision contient le SHA Git propre à A ou B.

Le runtime Python promouvable vient exclusivement du CPython standalone épinglé par
`deploy/runtime/ava-python-runtime.v1.json`. Son archive brute est conservée en `0444` sous
`.ava-artifacts/python-runtime.tar.gz`; son SHA, sa taille, ses cardinalités et sa projection
canonique sont recalculés. Les liens symboliques internes de l'archive sont résolus puis matérialisés
en fichiers réguliers indépendants et non inscriptibles sous `.python/`. La carte installée doit être
identique à cette projection, et `.venv/bin/python` doit être octet pour octet le binaire CPython
attesté. Les dépendances sont installées sans build ni réseau depuis
`deploy/runtime/ava-runtime-requirements.v1.txt`, avec hashes obligatoires et le wheel docopt
source-owned sous `deploy/runtime/wheels/`. L'attestation lie le binaire `uv`, sa version, ses
arguments, le lock, les requirements, le wheelhouse, toute la stdlib et toute la carte exhaustive de
`site-packages`. Aucun `.pth`, bytecode de site-packages, install editable ou fichier hors `RECORD`
n'est accepté. Les modules `anthropic`, `cryptography`, `httpcore` et `httpx` sont liés par chemin et
SHA à cette carte.

`causal-pair/v1` est produit sur un contrôleur disposant du dépôt Git autoritaire non shallow, des
deux archives source exactes et des deux attestations pré-épinglées. Il lit les arbres récursifs
complets avec `git ls-tree`, puis chaque blob exact avec `git cat-file` : `export-ignore` et
`export-subst` ne peuvent donc masquer un changement. Il compare ces cartes aux archives, prouve le
parent unique et le remplacement littéral, puis lie les équivalences Rust/wheel. Il porte toujours
`model_output_causality_claimed=false` : la paire isole le changement de code, elle ne transforme pas
les sorties stochastiques en preuve causale absolue.

Un consommateur hors ligne ne doit jamais croire les booléens ou SHA auto-déclarés du JSON. Son trust
root est le digest de causal pair pré-épinglé par le contrôleur GitOps qui a lui-même refait les
comparaisons Git/archive. Les attestations peuvent ensuite être copiées comme métadonnées : le
générateur de paire ne dépend ni d'un `.git` dans une release déployée, ni des réponses privées.

## Ordre de génération

Créer des répertoires de sortie possédés par l'opérateur, mode `0700`. Tous les fichiers d'entrée
doivent être réguliers, directs, non liés et bornés.

Les seules releases causales autoritaires sont `/var/lib/ava/releases/<git_sha>` et le pointeur
`/var/lib/ava/current`, tous deux sous parents root-owned non inscriptibles par l'utilisateur Ava.
Chaque commande de release part du bootstrap source-owned sous `python -I -S`; elle ne dépend ni de
`sitecustomize`, ni de `.pth`, ni du `PATH` de l'opérateur.
Le service utilise le même contrat :

```bash
"$release_b/.venv/bin/python" -I -S \
  "$release_b/ava_extensions/runtime_bootstrap.py" serve
```

1. Préparer A sans basculer `/var/lib/ava/current`, puis générer son attestation depuis
   l'interpréteur scellé de A :

```bash
"$release_a/.venv/bin/python" -I -S \
  "$release_a/ava_extensions/runtime_bootstrap.py" attest \
  --config /home/avalon/.openjarvis/config.toml \
  --evaluation-manifest-sha256 "$manifest_v3_sha256" \
  --output-dir "$evidence_dir"
```

2. Déployer B comme release courante, puis générer son attestation avec la même commande en remplaçant
   `release_a` par `release_b`. Le générateur relit `.ava-release-v1`, `.ava-ready`, les archives,
   la wheel, l'attestation Rust, le module treatment installé en lecture seule et la configuration
   Anthropic réellement déployée. `--config` est une assertion explicite et refuse tout chemin autre
   que `/home/avalon/.openjarvis/config.toml`; un fichier synthétique ne peut donc pas produire une
   attestation promotable. Il lit aussi le `manifest.v3.json` canonique sous la release,
   recharge tous ses fichiers épinglés, recoupe ses octets avec `source-tree.tar` et refuse que le
   seul SHA fourni en option serve de source de vérité. Il vérifie `/var/lib/ava/current` avant et après la
   lecture. A doit être
   `prepared_noncurrent`, B `active_current`. Le SHA pré-épinglé du manifeste d'évaluation est lié
   dans chacune des deux attestations avant que la paire n'existe.

3. Depuis le checkout Git autoritaire, construire la paire. Les SHA ci-dessous viennent du trust root
   GitOps ; ils ne sont pas auto-déduits à partir des fichiers mutables passés à la commande :

```bash
"$release_b/.venv/bin/python" -I -S \
  "$release_b/ava_extensions/runtime_bootstrap.py" causal-pair \
  --repository-root "$ava_git_checkout" \
  --baseline-source-archive "$release_a_source_tree" \
  --baseline-release-attestation "$attestation_a" \
  --baseline-release-attestation-sha256 "$attestation_a_sha256" \
  --candidate-source-archive "$release_b_source_tree" \
  --candidate-release-attestation "$attestation_b" \
  --candidate-release-attestation-sha256 "$attestation_b_sha256" \
  --evaluation-manifest-sha256 "$manifest_v3_sha256" \
  --output-dir "$evidence_dir"
```

Les générateurs n'affichent que `path` et `sha256`. Cette sortie prouve l'intégrité des octets, pas
leur autorité : CI/GitOps doit publier les pins retenus après sa propre revalidation.

4. Lancer A depuis l'interpréteur de A pendant que B reste la cible courante, puis B depuis
   l'interpréteur de B. Aucun argument ne choisit le rôle : le loader strict le dérive du treatment,
   des deux attestations et du côté de la causal pair.

```bash
"$release_a/.venv/bin/python" -I -S \
  "$release_a/ava_extensions/runtime_bootstrap.py" shadow \
  --configured-anthropic \
  --release-attestation "$attestation_a" \
  --release-attestation-sha256 "$attestation_a_sha256" \
  --peer-release-attestation "$attestation_b" \
  --peer-release-attestation-sha256 "$attestation_b_sha256" \
  --causal-pair "$causal_pair" \
  --causal-pair-sha256 "$causal_pair_sha256" \
  --output-dir "$baseline_output_dir"

"$release_b/.venv/bin/python" -I -S \
  "$release_b/ava_extensions/runtime_bootstrap.py" shadow \
  --configured-anthropic \
  --release-attestation "$attestation_b" \
  --release-attestation-sha256 "$attestation_b_sha256" \
  --peer-release-attestation "$attestation_a" \
  --peer-release-attestation-sha256 "$attestation_a_sha256" \
  --causal-pair "$causal_pair" \
  --causal-pair-sha256 "$causal_pair_sha256" \
  --output-dir "$candidate_output_dir"
```

Le runner n'accepte ni `--role`, ni backend URL, ni moteur fake, ni capability de test. Il vérifie
avant tout appel moteur le code, l'interpréteur, `.ava-release-v1`, l'attestation, la causal pair et
la cible fixe `/var/lib/ava/current`. Un objet opaque construit uniquement par ce loader ouvre le
scope runtime vérifié ; le module treatment recoupe son propre contenu, le Git exécuté et le côté de
la paire. A contourne alors entièrement le garde. B l'applique. Le scope est retiré à la fin et la
cible courante est relue avant publication.

Le seul backend promouvable est le `CloudEngine` Anthropic configuré par la release. Les quatre
sondes préalables, les 49 appels primaires et au plus une réparation par cas lié donnent exactement
`4 + 49 + R` appels modèle, avec `0 <= R <= 46`. L'observation A contient zéro `prepare`, `begin`,
`finish` et réparation. L'observation B contient 49 `prepare`, 46 `begin`, `R` `finish` et `R`
réparations. Elle enregistre seulement identifiant de cas, action, gates, identifiant de remplacement
et état de réparation ; aucun candidat rejeté, hash de candidat rejeté ou message complet.

5. Comparer les deux bundles avec toutes les preuves indivisibles et leurs pins externes :

```bash
"$release_b/.venv/bin/python" -I -S \
  "$release_b/ava_extensions/runtime_bootstrap.py" compare \
  --manifest "$release_b/ava_extensions/evals/relationship/data/manifest.v3.json" \
  --baseline "$baseline_bundle" \
  --baseline-sha256 "$baseline_bundle_sha256" \
  --baseline-release-attestation "$attestation_a" \
  --baseline-release-attestation-sha256 "$attestation_a_sha256" \
  --candidate "$candidate_bundle" \
  --candidate-sha256 "$candidate_bundle_sha256" \
  --candidate-release-attestation "$attestation_b" \
  --candidate-release-attestation-sha256 "$attestation_b_sha256" \
  --causal-pair "$causal_pair" \
  --causal-pair-sha256 "$causal_pair_sha256" \
  --report "$preliminary_report_path"
```

Le comparateur recharge chaque preuve et recalcule tous les liens ; il ne croit jamais un champ
`eligible` isolé. Les adjudications propriétaire et indépendante restent au format v2, l'ancrage
externe au format v1. Leur ajout exige leurs chemins et SHA pré-épinglés, ainsi que la clé publique
et son SHA ; un ensemble partiel est refusé.
Le rapport préliminaire est immuable. Après les deux adjudications et l'ancrage, relancer exactement
la comparaison vers un autre chemin afin de produire le rapport final :

```bash
"$release_b/.venv/bin/python" -I -S \
  "$release_b/ava_extensions/runtime_bootstrap.py" compare \
  --manifest "$release_b/ava_extensions/evals/relationship/data/manifest.v3.json" \
  --baseline "$baseline_bundle" --baseline-sha256 "$baseline_bundle_sha256" \
  --baseline-release-attestation "$attestation_a" \
  --baseline-release-attestation-sha256 "$attestation_a_sha256" \
  --candidate "$candidate_bundle" --candidate-sha256 "$candidate_bundle_sha256" \
  --candidate-release-attestation "$attestation_b" \
  --candidate-release-attestation-sha256 "$attestation_b_sha256" \
  --causal-pair "$causal_pair" --causal-pair-sha256 "$causal_pair_sha256" \
  --human-adjudication "$human_review" \
  --human-adjudication-sha256 "$human_review_sha256" \
  --independent-adjudication "$independent_review" \
  --independent-adjudication-sha256 "$independent_review_sha256" \
  --external-anchor "$external_anchor" \
  --external-anchor-sha256 "$external_anchor_sha256" \
  --anchor-public-key "$anchor_public_key" \
  --anchor-public-key-sha256 "$anchor_public_key_sha256" \
  --report "$final_report_path"
```

Pour v3, le code de sortie `0` signifie que les preuves nécessaires à l'adjudication sont prêtes ;
un rapport valide mais encore incomplet sort avec le code dédié `6`.

## DAG de preuves et promotion

Le graphe est strictement acyclique :

```text
manifest/v3
  -> release-attestation/v2 A et B
  -> causal-pair/v1
  -> responses/v3 A et B
  -> report/v3 + review-statement/v3
  -> adjudications/reviews v2
  -> external-anchor/v1
```

Chaque flèche signifie que l'artefact aval lie par SHA les artefacts déjà gelés en amont. Aucun
artefact historique n'est réécrit pour fermer le graphe, et aucune signature ne signe un document
qui contient sa propre empreinte. La promotion reste externe, explicite et fail-closed ; elle ne
modifie ni la persona, ni la mémoire, ni un système actif depuis ce banc.
