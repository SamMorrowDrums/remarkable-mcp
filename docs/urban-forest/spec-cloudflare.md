# reMarkable MCP on Cloudflare — phased spec (draft 2, 2026-11-11)

Urban Forest's plan for hosting this server. Draft 2 carries the owner's answers to the six decisions of draft 1 (section 5). Nothing here is built yet. It replaces the hosting sections of `spec-safety-and-permissions.md` and keeps that document's safety, permission and write-safety sections, which still apply.

Labels: **checked** = read in the named source on the date given; **follows** = follows from what was read; **guess** = a guess, with what would settle it. Words: *reading the tablet* (browse, read, render, export) · *changing the library* (push, create folder, move, rename, trash) · *editing a notebook's pages* (not in scope).

## 0. Where this starts

The server runs today on the owner's laptop: this fork, cloud transport, library changes on, reached by the Claude desktop app, by linked cloud sessions, and by ChatGPT through OpenAI's secure MCP tunnel. Reading a PDF's handwritten annotations works once the tablet has synced. Two known rough edges to carry forward: text extraction returned nothing for a real-text PDF on 2026-11-05 (render was fine), and the stroke parser logs newer-format warnings on Paper Pro notebooks. Both are bugs in the Python code, independent of hosting.

The point of the cloud version is the handoff: nothing on either user's computer, reachable with both computers asleep, by Claude and ChatGPT without a tunnel. The server needs no local machine in cloud mode — **checked** (the server's own status text: "no device needed, works from anywhere your token is valid").

## 1. Repository plan — a public fork

The upstream project is MIT, so a public fork is allowed as long as the MIT notice and Sam Morrow's copyright stay in the LICENSE file — **checked**.

**1.1 Where (decided).** `github.com/UrbanForestTech/remarkable-mcp` — public, MIT, a fork of `SamMorrowDrums/remarkable-mcp`, default branch `main`.

**1.2 Remotes and branches.** `origin` = the fork; `upstream` = SamMorrowDrums. `main` carries upstream plus Urban Forest's additions; upstream's `main` is merged in on a schedule the owner sets, never rebased over. A `release` branch is what Cloudflare deploys from (section 2.9); only the owner's merge moves it, so each deploy is one approval. Work happens on `lab/*` branches; nothing lands on `main` or `release` without the owner's approval. Fixes that are general (the text-extraction bug, parser warnings) go upstream as pull requests so the fork stays small; anything Urban Forest-specific stays in the fork.

**1.3 Checkouts.** The edit-and-test checkout is on the owner's desktop computer. A checkout inside a OneDrive folder worked for the owner's tools, but a Linux shell mounting that folder could not read its Python files ("Invalid argument"; **guess**: files-on-demand placeholders); if OneDrive ever interferes with git, move the checkout out of OneDrive.

**1.4 Layout.** Upstream's tree stays as it is. Added:
- `deploy/cloudflare/` — `Dockerfile`, `wrangler.jsonc`, `worker/` (the TypeScript gateway), `README.md` (how to deploy, every approval point marked).
- `docs/` — this spec, dated research notes, verification reports **with document names removed** (this repo is public: structure only — counts, formats, timings).
- `scripts/local/` — the PowerShell launchers for running the server on a Windows computer, with relative paths, so anyone can run the local version.
- `.gitignore` additions: `.cloud-cache/`, `logs/`, `cloud-verification/`, `verification/`, `tunnel/`, `.tools/`, `*.rmapi`, `.dev.vars`, `*-state.json`.

**1.5 What never enters the repo.** Tokens, the `.rmapi` file, registration codes, OAuth client secrets, tunnel keys, anyone's library listing or page contents. Secrets live in Cloudflare (`wrangler secret put`, or the dashboard) and, locally, in an ignored `.dev.vars` — **checked** (Cloudflare env-vars-and-secrets page).

**1.6 Checks and deploys (decided: cloud builds).** GitHub Actions runs upstream's own test suite on every pull request. Builds and deploys run in Cloudflare's **Workers Builds**, connected to this repo: it builds the `Dockerfile` and runs `wrangler deploy` in Cloudflare's own environment, so no Docker is needed on any computer — **checked** (Cloudflare Containers deploy page: "Dockerfile builds can run in the Workers Builds environment"). Its production branch is `release`. Connecting the repo, choosing the branch and entering secrets are the owner's clicks in the Cloudflare dashboard.

**1.7 README.** The fork's README says in its first lines what it is (Urban Forest's deployment of Sam Morrow's remarkable-mcp on Cloudflare), what changed, and that the reMarkable cloud route is reverse-engineered and may break on a vendor change.

## 2. Phase 1 — the Python server in a Cloudflare Container behind a Worker gateway

**2.1 Shape.** Two pieces, one deploy:
- **The gateway Worker (TypeScript).** Address `https://<name>.<account>.workers.dev/mcp` for the build; a custom domain is chosen at build step 5, before either client gets the address. It does sign-in (OAuth), decides which user this is, and forwards the MCP request to that user's container. It is a proxy, not an MCP server of its own: the Python server already speaks MCP over Streamable HTTP, so the Worker checks the bearer token and passes the HTTP request through to the container's port. **Follows** from the Containers model (a Worker forwards requests to a Durable Object bound to a container class, which starts the container and forwards to a port — **checked**).
- **The container.** This fork, built from a `Dockerfile` (`python:3.12-slim`, `linux/amd64` — required, **checked**), with `tesseract-ocr` installed from apt, running `python -m remarkable_mcp.cli --http --host 0.0.0.0 --port 8000` with the same settings as the local cloud launcher. One container instance per user, obtained with `getByName(userId)` — **checked** that per-name instances are the documented pattern. The user's reMarkable token is passed into the container as an environment variable at start — **checked** that secrets reach a container through `env` at `start()`, that they are visible to the container's process, and that a running process keeps its startup environment (a changed secret applies at the next start).

**2.2 Why this shape and not a TypeScript port.** The renderer is the hard part and it already works in Python; the port is a parser-and-renderer project with no mature TypeScript PDF renderer in hand. The port is phase 3, conditional (section 4).

**2.3 The token.** The server's command line reads `REMARKABLE_TOKEN` — **checked** (cli.py). What exactly that variable expects (the device token line of `.rmapi`, or the file's whole contents) is **not known**; read the cloud module before the first deploy and, if the variable is not enough, have the container's start script write the secret into `/root/.rmapi` before launching the server. Either way the secret is one `REMARKABLE_TOKEN_<USER>`, entered by the owner, never committed.

**2.4 Sign-in (decided: Microsoft 365).** Cloudflare's Workers OAuth Provider in front of the Worker — **checked** that this is Cloudflare's documented path for an authenticated remote MCP server (OAuthAuthorizationServer + OAuthResourceServer, token checked by the resource server). The upstream identity provider is Microsoft 365: Urban Forest accounts, through an app registration in Entra that the owner creates (no cost as far as known — settle in the Entra portal). Allowed users in phase 1: one e-mail address, checked in code; phase 2 moves this to a table. Claude connects as a custom connector by URL with "Register automatically" (dynamic client registration) — **checked** that Claude offers it; that Workers OAuth Provider supports it is **as far as known**, settle in its README; ChatGPT connects with OAuth in Developer Mode — **checked** as of 2025-09. The OAuth provider needs a KV namespace for its own state — **as far as known**; settle in its README.

**2.5 Storage.** KV: the OAuth provider's state only. No D1, no R2 in phase 1. The container's disk is scratch (render cache); whether it survives a sleep is **not known** — assume it does not, and let the server rebuild its cache. R2 for a shared render cache is a phase 2 option.

**2.6 Instance size and sleep.** Start on `basic` (1/4 vCPU, 1 GiB, 4 GB disk — **checked**); move to `standard-1` (1/2 vCPU, 4 GiB) if rendering a long PDF runs out of memory — **guess** on where the line is; the timing test shows it. `sleepAfter` ten minutes to start. Cold start length is **not known** (the docs give no figure; the Worker holds requests until the port is listening — **checked**); the timing test measures it, and it is the number that decides whether phase 3 ever happens.

**2.7 Costs.** Workers Paid: $5/month (approved 2026-11-11). It includes 375 vCPU-minutes, 25 GiB-hours of memory and 200 GB-hours of disk per month; beyond that $0.000020 per vCPU-second and $0.0000025 per GiB-second — **checked** (pricing page, 2026-11-05). Arithmetic, labeled as such: a 1 GiB container awake around the clock would be about 720 GiB-hours, roughly $6.50 over the included amount; one that sleeps after ten minutes and serves a few dozen calls a day stays inside the included amounts. Egress is far below the included 1 TB. reMarkable Connect: both users have it. A custom domain: a cost only if a new domain is bought; decided at step 5.

**2.8 Permissions in phase 1.** Reading plus library changes, as on the local server. Tool scoping and folder scoping are phase 2. Delete stays Trash-only (the server's behavior; permanent delete is not exposed — **checked** in the status text).

**2.9 Build order, with the approval points.**
1. Fork exists (done). On the desktop checkout: `origin` = fork, `upstream` = original; `.gitignore`, `scripts/local/` and `docs/` additions on a branch. Merge to `main`: one approval.
2. `deploy/cloudflare/` on a branch: `Dockerfile`, `wrangler.jsonc`, the gateway Worker with a temporary single bearer token (no OAuth yet). Tests that need no container: the Worker's own tests; the server's test suite in GitHub Actions.
3. Cloudflare: Workers Paid (approved); Workers Builds connected to the repo with `release` as the production branch; the token secret entered by the owner. First merge into `release`: one approval — this is the first deploy and the build of the image in Cloudflare's environment. **The timing test:** first call after sleep, and a page render, timed and logged; the local verification client run against the gateway with the temporary token. Half a day.
4. OAuth added on a branch; the Entra app registration is the owner's click; allowlist of one. Merge into `release`: one approval.
5. **Custom domain decided here**: a subdomain of a domain Urban Forest owns, or a new one (a cost, one approval). Then the Claude custom connector added by URL (approval: name, address, what it exposes) and the ChatGPT connector the same way. The verification client run through the gateway with OAuth.
6. Local server kept as fallback; OpenAI tunnel retired when ChatGPT works through the gateway.

**2.10 Success for phase 1.** Claude and ChatGPT reach the gateway; the signed-in user resolves to his own token; status, browse, read, image, export, push, create folder, move, rename, trash work through it; no local process, no USB, no developer mode; library changes confirmed by a fresh browse; Trash is recoverable; no token ever appears in a tool result or a log; a second user can be added without redesign (per-user container by name, per-user secret).

## 3. Phase 2 — hardening and the second user

Kept from the original spec: audit log of library changes (D1), permission scopes (`remarkable.read`, `remarkable.write`) enforced in the Worker by filtering MCP `tools/call` by tool name before forwarding — **follows** from the proxy shape; **guess** that no other method needs filtering, settle by reading the MCP method list; folder scope (either the server's own `REMARKABLE_ROOT_PATH` per container — **checked** the server reads it — or a Worker-side check); rate limiting; cache invalidation; an account page where a person enters a reMarkable desktop code to link a tablet (the registration itself runs inside that person's container through the server's `--register` path — **checked** that the flag exists; how to drive it non-interactively is **not known**); the second user's row, container and secret. Writes are naturally serialized per user because each user has one container process; two users never share one. The second user has a reMarkable and a Connect subscription, so phase 2 can start as soon as phase 1 is stable.

## 4. Phase 3 — the Worker rewrite, conditional

**4.1 Trigger.** Phase 3 starts only if, after four weeks of real use on phase 1 or 2, the log shows that cold starts or cost hurt the assistants' work (a first-call wait users notice, or a monthly bill clearly above the $5 plan). Otherwise it stays on the shelf. A keep-warm ping is worth trying before any rewrite, because it costs cents and removes the only known pain.

**4.2 Order of attempts.** (a) **Python Workers first.** Python Workers are generally available (Pyodide/WebAssembly) and take pure-Python and Pyodide-built packages, declared in `pyproject.toml`, bundled at deploy — **checked** (2026-11-05). `rmscene` is pure Python — **checked** (wheel tag `py3-none-any`). `mcp` and `requests`: **not known** whether they run under Pyodide (networking must go through the Worker's fetch; `requests` needs the Pyodide patch); `pymupdf` is a C library and **likely unavailable** — **guess**; Tesseract is a separate binary and **will not run** — **follows**. So the spike is: import the fork under Pyodide, replace PDF rendering (the render path, not the parser) and drop OCR to a hosted OCR call or to "images only". One day. (b) **TypeScript second**, only if (a) fails: the cloud transport is small and portable; the parser can start from `remarkable-rm` on npm (v6 support claimed; six months old; maintainer unknown — **checked** for existence only) with `rmscene` as the oracle in tests; PDF rendering needs a JavaScript renderer that works in a Worker — **not known** which. Exit test for either: the same verification suite the Container passed, run for two weeks in parallel, then the Container retired.

**4.3 A second opinion** (a fresh session with a blind brief, 2026-11-05) argued for keeping the Container as the permanent home and spending the effort on a well-tested transport layer plus a stable internal interface, with cold starts cut by a keep-warm ping or a render cache — "a few dozen lines, not a rewrite" — and, if a rewrite is ever needed, Python Workers first because it keeps one codebase. Its pre-mortem for a rewrite: "reMarkable changed the format, the Python side got fixed because rmscene's community fixed it, and the TypeScript side stalled because nobody who could fix it was looking." This spec follows that advice: phase 3 is written down and gated.

## 5. Decisions — all answered 2026-11-11

1. Repo: `UrbanForestTech/remarkable-mcp`, public fork.
2. Sign-in: Microsoft 365 through an Entra app registration.
3. Second user: has a reMarkable and Connect; phase 2 can follow phase 1 directly.
4. Address: `workers.dev` for the build; a custom domain chosen at step 5.
5. Builds: no Docker on any computer; Cloudflare Workers Builds builds and deploys from the `release` branch.
6. Cost: Workers Paid $5/month — approved.

Still open before step 3: what `REMARKABLE_TOKEN` expects; whether Workers OAuth Provider needs KV and supports dynamic client registration; which Cloudflare account.

## 6. Sources
- Cloudflare: Containers overview, Get started, Limits and instance types, Pricing, Env vars and secrets (developers.cloudflare.com, 2026-11-05); Containers deploy page — Workers Builds (2026-11-11); remote MCP server guide (2026-11-03); Python Workers packages page (2026-11-05).
- Claude custom connectors (claude.com/docs/connectors/custom, 2026-11-03); ChatGPT Developer Mode (Auth0 post, 2025-09-11).
- This fork's `cli.py`, `pyproject.toml`, `LICENSE`; the server's `remarkable_status` output (2026-11-04).
- rmscene on PyPI (2026-11-05).
