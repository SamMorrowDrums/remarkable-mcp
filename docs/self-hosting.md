# Auto-hébergement — connecter Claude à votre reMarkable sans rien installer sur votre ordinateur

Ce guide déploie `remarkable-mcp` sur votre propre serveur, exposé en HTTPS avec
authentification OAuth, pour l'ajouter comme **connecteur personnalisé** dans
Claude. Résultat : vos notes sont accessibles depuis claude.ai, l'application
mobile ou Claude Desktop, sans rien installer localement, et sans confier vos
documents à un service tiers.

Le CLI du projet ne parle que stdio (le client doit alors tourner sur la même
machine). Les fichiers de `deploy/` ajoutent la couche manquante : transport
HTTP + serveur d'autorisation OAuth mono-utilisateur.

**Temps estimé :** 45 à 60 minutes.

---

## Ce qu'il vous faut

| | |
|---|---|
| Un VPS | 1 vCPU / 1 Go suffisent. Debian 12 ou Ubuntu 24.04. |
| Un nom de domaine | Un sous-domaine suffit, ex. `rm.mondomaine.fr` |
| Un abonnement reMarkable Connect | Sans lui, rien n'est synchronisé dans le cloud |

---

## Étape 1 — Le DNS

Créez un enregistrement **A** pointant votre sous-domaine vers l'IP de votre VPS,
puis vérifiez la propagation :

```bash
dig +short rm.mondomaine.fr
```

La commande doit renvoyer l'IP de votre serveur. Attendez que ce soit le cas
avant de continuer : Caddy en a besoin pour obtenir le certificat TLS.

Ouvrez les ports 80 et 443 dans le pare-feu de votre hébergeur.

## Étape 2 — Préparer le serveur

Connectez-vous en SSH, puis :

```bash
sudo apt update && sudo apt install -y git curl debian-keyring debian-archive-keyring apt-transport-https

# uv (le lanceur Python)
curl -LsSf https://astral.sh/uv/install.sh | sudo env UV_INSTALL_DIR=/usr/local/bin sh

# Caddy (reverse proxy + TLS automatique)
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy
```

Créez l'utilisateur de service et les répertoires :

```bash
sudo useradd --system --home /var/lib/remarkable-mcp --create-home remarkable
sudo git clone https://github.com/SamMorrowDrums/remarkable-mcp /opt/remarkable-mcp
sudo chown -R remarkable:remarkable /opt/remarkable-mcp /var/lib/remarkable-mcp
```

> Le rendu d'images de page nécessite Cairo. Si vous voulez que Claude puisse
> afficher vos pages : `sudo apt install -y libcairo2`. L'extraction de texte
> (PDF, EPUB, texte tapé) fonctionne sans.

## Étape 3 — Le jeton reMarkable

Générez un code sur [my.remarkable.com/device/desktop/connect](https://my.remarkable.com/device/desktop/connect)
— il est à usage unique et expire en quelques minutes. Puis, **sur le serveur** :

```bash
sudo -u remarkable env HOME=/var/lib/remarkable-mcp \
  uv run --directory /opt/remarkable-mcp python -m remarkable_mcp.cli --register VOTRE_CODE
```

Le jeton est enregistré dans `/var/lib/remarkable-mcp/.rmapi`. Verrouillez-le :

```bash
sudo chmod 600 /var/lib/remarkable-mcp/.rmapi
```

Vérifiez que la bibliothèque est lisible :

```bash
sudo -u remarkable env HOME=/var/lib/remarkable-mcp \
  uv run --directory /opt/remarkable-mcp python -c \
  "from remarkable_mcp.api import get_rmapi; print(len(get_rmapi().get_meta_items()), 'documents')"
```

## Étape 4 — La phrase secrète

C'est le seul secret entre internet et votre bibliothèque. Générez-la, ne
l'inventez pas :

```bash
openssl rand -base64 32
```

Créez le fichier d'environnement (lisible par root uniquement) :

```bash
sudo tee /etc/remarkable-mcp.env > /dev/null <<'EOF'
RMMCP_PUBLIC_URL=https://rm.mondomaine.fr
RMMCP_PASSPHRASE=collez-ici-la-phrase-generee
EOF
sudo chmod 600 /etc/remarkable-mcp.env
```

Conservez la phrase dans votre gestionnaire de mots de passe : elle vous sera
demandée à chaque fois que vous connectez un nouveau client Claude.

## Étape 5 — Lancer le service

```bash
sudo cp /opt/remarkable-mcp/deploy/remarkable-mcp.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now remarkable-mcp
sudo systemctl status remarkable-mcp --no-pager
```

Le service écoute sur `127.0.0.1:8080` — inaccessible depuis l'extérieur, c'est
voulu. Vérifiez :

```bash
curl -s http://127.0.0.1:8080/healthz   # doit répondre: ok
```

## Étape 6 — Le reverse proxy et le TLS

```bash
sudo cp /opt/remarkable-mcp/deploy/Caddyfile /etc/caddy/Caddyfile
sudo sed -i 's/rm\.example\.org/rm.mondomaine.fr/' /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

Caddy obtient le certificat automatiquement (quelques secondes). Vérifiez depuis
votre machine :

```bash
curl -s https://rm.mondomaine.fr/.well-known/oauth-authorization-server | head -c 200
```

Vous devez voir un JSON décrivant les points d'entrée OAuth. Si c'est le cas,
tout est en place.

## Étape 7 — Ajouter le connecteur dans Claude

Dans **claude.ai → Réglages → Connecteurs → Ajouter un connecteur personnalisé**,
saisissez l'URL du point d'entrée MCP :

```
https://rm.mondomaine.fr/mcp
```

Claude s'enregistre tout seul auprès de votre serveur (enregistrement dynamique),
puis ouvre votre page de connexion. Saisissez la phrase secrète de l'étape 4 :
vous êtes redirigé vers Claude, connecté.

Testez : *« Liste mes documents reMarkable récents »*.

---

## Ce que ce déploiement fait — et ne fait pas

**Le serveur démarre en lecture seule.** Claude peut lire, chercher et afficher
vos documents, mais pas les modifier. C'est délibéré : un serveur exposé sur
internet est exactement l'endroit où un appel d'outil destructeur non surveillé
fait le plus de dégâts, et le contenu d'un document lu par le modèle peut
contenir des instructions. Pour activer l'écriture (téléversement, dossiers,
renommage, suppression), ajoutez `RMMCP_ALLOW_WRITES=1` dans
`/etc/remarkable-mcp.env` et redémarrez — en connaissance de cause, et après
avoir lu [la revue de sécurité](security-review.md).

**L'écriture manuscrite est le point faible.** Le mode OCR le plus élégant du
projet demande au modèle du client de lire l'image (« sampling ») — les
connecteurs claude.ai ne le prennent pas en charge. Il vous reste Google Vision
(clé API, facturé à la page, vos notes transitent par Google) via
`GOOGLE_VISION_API_KEY` dans le fichier d'environnement, ou Tesseract
(`apt install tesseract-ocr`, gratuit mais médiocre sur le manuscrit). Le texte
tapé, les PDF et les EPUB se lisent parfaitement sans OCR.

**Authentification mono-utilisateur.** Une phrase secrète, un seul sujet, pas de
comptes ni de rôles. Si vous devez ouvrir l'accès à plusieurs personnes,
remplacez le serveur d'autorisation intégré par un fournisseur d'identité réel
et faites tourner FastMCP en simple serveur de ressources (`token_verifier=`).

---

## Exploitation

**Les journaux**

```bash
sudo journalctl -u remarkable-mcp -f      # le service
sudo tail -f /var/log/caddy/remarkable-mcp.log   # les accès HTTP
```

Surveillez les tentatives de connexion échouées (`Failed login attempt` dans le
journal du service). Cinq échecs déclenchent un verrouillage de 15 minutes.

**Le cache disque** grandit sans limite : `/var/lib/remarkable-mcp/cache`.
Surveillez-le, ou désactivez-le avec `REMARKABLE_DISABLE_CACHE=1`.

**Mettre à jour**

```bash
cd /opt/remarkable-mcp && sudo -u remarkable git pull
sudo systemctl restart remarkable-mcp
```

**Révoquer un accès.** Pour déconnecter tous les clients d'un coup :

```bash
sudo rm /var/lib/remarkable-mcp/auth/auth-state.json
sudo systemctl restart remarkable-mcp
```

Changez aussi la phrase secrète dans `/etc/remarkable-mcp.env` si vous pensez
qu'elle a fuité.

---

## En cas de problème

| Symptôme | Cause probable |
|---|---|
| Caddy n'obtient pas de certificat | DNS pas encore propagé, ou ports 80/443 fermés |
| `502 Bad Gateway` | Le service est arrêté — `systemctl status remarkable-mcp` |
| Claude dit « impossible de se connecter » | L'URL doit se terminer par `/mcp` |
| La page de connexion s'affiche mais la redirection échoue | `RMMCP_PUBLIC_URL` ne correspond pas au domaine réel |
| `0 documents` à l'étape 3 | Jeton expiré, ou pas d'abonnement Connect actif |
