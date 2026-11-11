# Research note — verification of the original spec (2026-11-03)

Every claim is labeled: **checked** (read in the named source that day), **follows** (follows from what was read), or **guess** (a guess, with the source that would settle it). No tablet contents are in this note.

## 1. What the spec gets right

- **The reference project exists and matches the spec's tool list.** `SamMorrowDrums/remarkable-mcp`, MIT, Python, latest 2.1.0 released 2026-08-17 on PyPI. Tools: status, browse, recent, search, read, image, export, upload, mkdir, move, rename, delete, plus markdown_to_pdf, canvas, refresh (SSH only) and author (SSH only). Cloud mode gives "full document and folder management." — **checked** (PyPI page; GitHub README).
- **Delete is a move to Trash in the reference.** — **follows** (README describes recoverable delete).
- **A remote MCP server with OAuth on Cloudflare Workers is a supported, documented path.** Cloudflare's guide uses `createMcpHandler` from the Agents SDK with Workers OAuth Provider, Streamable HTTP, and says the server "needs no Durable Object or session." `McpAgent` is marked deprecated. — **checked** (developers.cloudflare.com remote MCP guide).
- **Claude accepts a custom connector by URL on every plan.** OAuth options: Claude's published identity (recommended), dynamic client registration, or your own client ID. On a Team or Enterprise plan an Owner adds it for the organization and members connect with their own account. — **checked** (claude.com/docs/connectors/custom).
- **ChatGPT accepts a remote MCP server with OAuth and allows write tools**, in Developer Mode (beta) on Plus or Pro, with each tool call confirmed by the person. — **checked** as of a 2025-09-11 Auth0 post; **guess** that it still holds today (source to settle: OpenAI's connector docs).
- **Cloud mode needs a reMarkable Connect subscription** ($3.99/month after a 50-day trial). — **checked** (README; remarkable.com pricing page).

## 2. What the spec states as fact that is not settled

- **"reMarkable Cloud API" is not official.** reMarkable's developer portal (updated 2026-09-24) documents device SDKs, developer mode and recovery; no cloud API. The community documentation calls itself unofficial. `juruen/rmapi` was archived 2024-07-02; `ddvk/rmapi` carries on and mentions root-index schema versions 3 and 4, so the protocol has changed more than once. — **checked**. The transport is reverse-engineered and can break on a vendor change; that is the biggest single risk in the plan.
- **"Developer mode factory-resets the tablet" is true only for the Paper Pro line.** reMarkable's SDK page: "there is currently no concept of a developer mode for reMarkable 1 or 2." The community guide says SSH on a reMarkable 2 works with "no configuration required". Paper Pro, Paper Pro Move and Paper Pure need developer mode, which factory-resets and weakens device security. — **checked**.
- **A TypeScript port of the page renderer is a real project, not a transcription.** The reference renders with `rmscene` (Python, .rm v6 parser, experimental write support) and PyMuPDF (C library; not expected to run in a Worker — **guess**). An npm package `remarkable-rm` (v1.1.0, about six months old) claims v6 parsing to SVG; its maturity is unknown. — **checked** for existence, **guess** for fitness.
- **Python Workers are generally available** on Cloudflare (Pyodide/WebAssembly), but threading and multiprocessing do not work and C-extension packages are limited to what Pyodide ships. — **checked** (Simon Willison, 2026-09-21). **Cloudflare Containers are GA since 2026-04-13** on the Workers Paid plan ($5/month plus per-second active CPU), reached from a Worker by service binding, and sleep when idle. — **checked** (cloudmagazin 2026-05-10; later confirmed on Cloudflare's own docs).

## 3. Native notebook editing

Short answer: in-place editing of an existing notebook page is not available by any tested route. Append is available by one route today, and possibly by a second, untested one.

- **Through the cloud, the reference project cannot author.** `remarkable_author` (draw strokes, add page, create notebook) is "SSH only," and every verb in its description is "append." — **checked** (README).
- **Over SSH, append works today** — on a reMarkable 2 with no reset; on a Paper Pro only after developer mode and a factory reset. The server has to reach the tablet on its network, so this is a local host. — **checked** for the model facts; **follows** for the hosting consequence.
- **Cloud write-back of a modified notebook is plausible but unproven.** The pieces exist separately: `.rmdoc` is a zip the web app exports; `rmapi` accepts it for upload when renamed `.zip`; `ddvk/rmapi` has `--force` ("completely replace an existing document"); `rmscene` keeps unparsed data "to allow for round-trip saving"; `rmstrokes` builds `.rmdoc` files with strokes from scratch (reMarkable 2 only, "early days"); `rmc` makes a `.rm` page from text. Nobody found has edited a page, re-uploaded it, and had the tablet accept it. — **checked** for each piece; **guess** that the chain works; settle with one experiment on a throwaway notebook. The risk is overwriting edits made on the tablet since the download.
- **The format is undocumented.** `rmstrokes`: "The .rmdoc and .rm v6 formats aren't officially documented." Parser warnings on newer firmware are expected. — **checked**.

Alternatives that give most of the value without editing a page: (1) companion documents — the AI never touches the notebook; it pushes a new document beside it (a PDF from `remarkable_markdown_to_pdf`, or a typed-text page via `rmc`), the person annotates it, the connector reads it back; (2) append over SSH locally, if the tablet is a reMarkable 2; (3) the cloud write-back experiment, later; (4) typed text instead of ink — notebooks hold typed text natively and `rmc` writes that block type, with the same risks as (3).

## 4. Second opinion on hosting (fresh session with a blind brief, 2026-11-03)

Strongest form of the original plan: a hosted gateway is the only shape that serves Claude and ChatGPT from anywhere and lets a second user join without running anything; a TypeScript port puts everything in one native runtime. Strongest different approach: do not port — run the Python reference unchanged in a Cloudflare Container behind a thin Worker that does sign-in and a short allowlist; no D1, R2, scope system or audit table in v1. Pre-mortem for the port: "the TypeScript renderer never matched the Python one closely enough, nobody could say why, and it stalled before daily use." Pre-mortem for the Container: "the sleeping container turned every first call into a long wait, or reMarkable changed the protocol, which kills both plans equally." Cheap evidence: look for a mature TypeScript .rm v6 parser (one hour); put the untouched Python server in a Container behind a hello-world Worker and time one page render (half a day and $5). Recommendation: change the hosting shape, keep the scope. The owner adopted this; see `spec-cloudflare.md`.

## Sources read
- https://pypi.org/project/remarkable-mcp/ and https://github.com/SamMorrowDrums/remarkable-mcp
- https://github.com/ricklupton/rmscene · https://pypi.org/project/rmc/ · https://github.com/avncharlie/rmc-scrybbling · https://github.com/marcinkevicius/rmstrokes
- https://github.com/juruen/rmapi (archived) · https://github.com/ddvk/rmapi · https://github.com/ddvk/rmfakecloud · https://akeil.de/posts/remarkable-cloud-api/
- https://developer.remarkable.com/documentation · /sdk · /developer-mode · https://remarkable.guide/guide/access/ssh.html · https://remarkable.com/shop/connect/pricing
- https://developers.cloudflare.com/agents/guides/remote-mcp-server/ · https://simonwillison.net/2026/Sep/21/cloudflare-python-worker/ · https://www.cloudmagazin.com/en/2026/05/10/cloudflare-containers-outgrowing-workers/
- https://claude.com/docs/connectors/custom · https://auth0.com/blog/add-remote-mcp-server-chatgpt/ · https://www.pkgstats.com/pkg:remarkable-rm
