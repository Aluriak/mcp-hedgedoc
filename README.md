# mcp-hedgedoc

Serveur MCP pour HedgeDoc 1.x — lit et crée des notes via un compte de
service unique, avec un vrai mode lecture seule pour la paire de
variantes `ro`/`rw` de LibreChat.

## Ce que ce serveur peut et ne peut pas faire

HedgeDoc 1.x n'a **aucune API à jetons** : toute action passe par un
cookie de session obtenu via un vrai login. Ce serveur agit donc toujours
sous un **compte de service partagé** — jamais sous l'identité réelle de
la personne qui pose la question dans LibreChat. Toute note créée, toute
action listée dans `/history`, apparaît sous ce compte dans l'historique
HedgeDoc.

Il n'y a pas d'outil `update_note` ni `delete_note`, et ce n'est pas un
oubli : `POST /new/{alias}` sur un alias déjà existant renvoie **409
Conflict** (vérifié sur le code source de HedgeDoc 1.11.1 et 1.12.0,
identique dans les deux). La modification du contenu d'une note existante
ne se fait que via le canal Socket.IO temps réel de l'éditeur web, que ce
serveur n'implémente pas.

## Outils exposés

| Outil | Lecture seule | Description |
|---|---|---|
| `read_note` | oui | Contenu Markdown brut d'une note |
| `get_note_info` | oui | Titre, description, vues, dates |
| `get_note_revisions` | oui | Liste des révisions sauvegardées |
| `get_note_revision_content` | oui | Contenu à une révision donnée |
| `whoami` | oui | Identité de la session actuelle (toujours le compte de service) |
| `list_history` | oui | Notes récemment consultées/épinglées par le compte de service |
| `create_note` | non | Crée une **nouvelle** note (jamais une modification) |

## Variables d'environnement

| Variable | Obligatoire | Défaut | Description |
|---|---|---|---|
| `HEDGEDOC_URL` | oui | — | URL de base, ex. `https://hedgedoc.example.org` |
| `HEDGEDOC_AUTH_MODE` | non | `local` | `local`, `oidc`, ou `cookie` |
| `HEDGEDOC_READ_ONLY` | non | `false` | `true` pour la variante ro |
| `MCP_SERVER_PORT` | non | `8000` | Port d'écoute HTTP |
| `HEDGEDOC_TIMEOUT_SECONDS` | non | `15` | Timeout des requêtes HTTP |
| `REQUESTS_CA_BUNDLE` | non | — | CA interne, si nécessaire |

Selon `HEDGEDOC_AUTH_MODE` :

| Mode | Variables requises |
|---|---|
| `local` | `HEDGEDOC_EMAIL`, `HEDGEDOC_PASSWORD` (compte HedgeDoc local, ex. créé via `bin/manage_users`) |
| `oidc` | `HEDGEDOC_OIDC_USERNAME`, `HEDGEDOC_OIDC_PASSWORD` (compte IdP **dédié**, pas celui d'une vraie personne), et éventuellement `HEDGEDOC_OIDC_LOGIN_PATH` (défaut `/auth/oauth2`) |
| `cookie` | `HEDGEDOC_SESSION_COOKIE` (cookie de session déjà valide — surtout utile en debug) |

Voir la docstring de `auth.py` pour le détail de ce que fait réellement le
mode `oidc` (script de connexion via le formulaire du IdP) et de sa
fragilité connue (thème de connexion personnalisé, 2FA, actions requises
sur le compte).

## Utilisation avec podman-compose

Une seule image, les deux variantes changent uniquement `HEDGEDOC_READ_ONLY`
(exemple de composition, à adapter à votre registre) :

```yaml
{% for v in librechat_mcp_servers.hedgedoc.variants %}
  mcp-hedgedoc-{{ v }}:
    image: ghcr.io/votre-org/mcp-hedgedoc:latest
    container_name: mcp-hedgedoc-{{ v }}
    environment:
      - HEDGEDOC_URL={{ hedgedoc_addr }}
      - HEDGEDOC_AUTH_MODE=local
      - HEDGEDOC_EMAIL={{ mcp_hedgedoc_bot_email }}
      - HEDGEDOC_PASSWORD={{ mcp_hedgedoc_bot_password }}
      - HEDGEDOC_READ_ONLY={{ 'true' if v == 'ro' else 'false' }}
      - REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-bundle.crt
    volumes:
      - /etc/ssl/certs/ca-bundle.crt:/etc/ssl/certs/ca-bundle.crt:ro
    restart: unless-stopped

{% endfor %}
```

## Construire et publier l'image

L'image est publiée sur GHCR par la CI GitHub Actions (voir
`.github/workflows/ci.yml`) : tag immuable par commit, plus `latest` sur
la branche par défaut. Pour construire et pousser manuellement :

```bash
podman build -t ghcr.io/votre-org/mcp-hedgedoc:latest -f Containerfile .
podman login ghcr.io
podman push ghcr.io/votre-org/mcp-hedgedoc:latest
```

## Vérifier une image après construction

```bash
scripts/smoke_test.sh ghcr.io/votre-org/mcp-hedgedoc:latest
```

Lance le conteneur en mode lecture seule contre une URL bidon, vérifie que
les 6 outils de lecture apparaissent et que `create_note` est bien
rejeté — sans jamais toucher à une vraie instance HedgeDoc. Voir le script
pour le détail.

## Tests

```bash
pip install -e ".[dev]"
pytest
```

24 tests, tous hors-réseau (mocks HTTP via `responses`, page IdP
synthétique pour le scraping OIDC) sauf le smoke test du conteneur
ci-dessus, qui est volontairement séparé du reste.

## Voir aussi

`AGENTS.md` pour les conventions du dépôt (comment ajouter un outil,
pourquoi certains choix ont été faits) et les limitations connues de
HedgeDoc 1.x sur lesquelles ce projet s'appuie.

## Licence

GPL-3.0-or-later — voir [LICENSE](LICENSE).