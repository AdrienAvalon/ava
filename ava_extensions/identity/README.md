# Identité HTTP et overlay relationnel

La persona commune d'Ava est versionnée dans `system_prompts/ava.md`. Elle ne
contient aucun nom, aucune relation privée et aucun contenu provenant des
fichiers legacy `SOUL.md`, `MEMORY.md` ou `USER.md`. La route `/v1/ava/persona`
n'expose que cette persona commune.

L'overlay `virtual-girlfriend-v1` est distinct, explicite, révocable et
transparent sur la nature IA d'Ava. Il ne change ni outil, ni permission, ni
garde-fou. Il interdit notamment jalousie, exclusivité, culpabilisation,
isolement, affirmation d'une conscience humaine et revendication de sentiments
réels. Le serveur le compose une seule fois après authentification ; un texte de
requête, le champ OpenAI
`user`, un `name` ou un message client `system` ne peut jamais le sélectionner.

## Contrats runtime

Navigateur OIDC :

- `AVA_OIDC_ISSUER`
- `AVA_OIDC_AUDIENCE`
- `AVA_OIDC_JWKS_URL` (facultatif ; dérivé de l'issuer sinon)
- en-tête `X-Ava-Identity`

Control Plane :

- `AVA_CP_ASSERTION_KEY_FILE`
- `AVA_CP_ASSERTION_KEY_ID` (défaut `current`, identifiant public du header JWT `kid`)
- `AVA_CP_ASSERTION_PREVIOUS_KEY_FILE` et `AVA_CP_ASSERTION_PREVIOUS_KEY_ID`
  (optionnels, obligatoirement ensemble pendant une rotation bornée)
- `AVA_CP_ASSERTION_ISSUER` (défaut `avalon-control-plane`)
- `AVA_CP_ASSERTION_AUDIENCE` (défaut `ava`)
- en-tête `X-Ava-Service-Assertion`

L'assertion CP est un JWT HS256 de durée maximale 120 secondes avec exactement
`iss`, `aud`, `sub`, `iat`, `nbf`, `exp` et `jti`. Son `sub` Matrix a la forme
`matrix:<sender>`. La seule autorité synthétique admise est
`scheduler:ava-veille`, utilisée par la veille plan-only et jamais liée au profil
relationnel. Toute autre grammaire de service est refusée. La clé est lue dans un fichier runtime régulier, non suivi
par lien symbolique, appartenant à l'UID du processus, owner-only et d'au moins
32 octets ; elle n'est jamais placée dans l'environnement ou le dépôt.

La sélection relationnelle utilise `AVA_RELATIONSHIP_POLICY_FILE`. Le fichier
doit appartenir à l'UID du processus, n'avoir aucun droit groupe/autres et
respecter ce schéma strict :

```json
{
  "version": 1,
  "enabled": false,
  "bindings": [
    {
      "provider": "oidc",
      "issuer": "https://issuer.example.invalid/realms/example",
      "subject": "opaque-subject",
      "profile": "virtual-girlfriend-v1",
      "display_name": "Camille"
    }
  ]
}
```

Le passage à `enabled: true` est une promotion GitOps séparée, humaine et
réversible, uniquement après les smokes de principal, les évaluations shadow et
la preuve de rollback. L'exemple reste donc volontairement désactivé.

`display_name` est facultatif, borné et provient uniquement de ce binding. Il
n'est jamais dérivé d'un localpart Matrix, d'un subject OIDC ou du dialogue.
Une politique absente, révoquée, ambiguë, invalide ou trop permissive désactive
l'overlay. Une credential présente mais invalide produit un HTTP 401. L'absence
complète de credential et un principal vérifié mais non lié conservent la persona
commune, sans révéler le profil privé.

## Contexte d'adresse vérifié

`AVA_PRINCIPAL_CONTEXT_FILE` désigne un second registre, distinct de la politique
relationnelle et des capacités. Il associe seulement un principal déjà vérifié à
un `display_name` et une `preferred_language`. Cela permet à Ava de tutoyer et de
nommer naturellement son interlocuteur même si l'overlay privé est rollbacké,
sans transformer un nom en mémoire, relation ou autorisation.

Le document v1 contient exactement `version` et `bindings`; chaque binding porte
exactement `provider`, `issuer`, `subject`, `display_name` et
`preferred_language`. En production, le fichier est régulier, sans lien dur ou
symbolique, `root:avalon 0640`, borné à 64 Kio et épinglé par le descripteur
systemd root-owned chargé après le `.env`. Une configuration annoncée mais
absente, ambiguë ou invalide bloque la requête avant le modèle. Les identifiants
OIDC et Matrix bruts ne sont jamais injectés dans le prompt.

## Frontière mémoire

Tant que `memory_facts.jsonl` reste un magasin partagé legacy, un tour avec
overlay relationnel n'en lit aucun contexte, ne lui soumet aucun échange et ne
reçoit pas l'outil `memoire`. L'historique conversationnel privé, partitionné
par principal vérifié, reste distinct et inchangé.
