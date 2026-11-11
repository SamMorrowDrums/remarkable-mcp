# reMarkable Cloud MCP Gateway — original architecture spec (2026-11-03)

The owner's original spec for the hosted gateway, lightly condensed. Its **hosting sections (3, 8, 14, 25–27, 30) are superseded by `spec-cloudflare.md`**: the server is not ported to a Worker in phase 1; it runs unchanged in a Cloudflare Container behind a thin gateway Worker. Everything about identity, credentials, permissions, folder scope, safety, write safety, concurrency, caching and failure handling still applies and is the reference for the gateway's behavior. Folder names in the examples are illustrative.

## 1. Objective
Build a production-ready remote MCP integration for reMarkable that uses reMarkable Cloud as the tablet transport; exposes a single remote MCP endpoint usable by ChatGPT, Claude, and other MCP clients; runs the public MCP gateway on Cloudflare; supports per-user authentication and isolated reMarkable credentials; preserves the tested cloud capabilities (browse, read, render, export, upload, create folders, move, rename, trash); can later support multiple Urban Forest users, each connected to their own reMarkable account; and can later add native notebook editing without changing the client-facing MCP architecture. The upstream `SamMorrowDrums/remarkable-mcp` implementation remains the reference implementation and regression-test target.

## 2. High-level architecture
MCP clients → (Streamable HTTP + OAuth) → Cloudflare MCP gateway (`/mcp` endpoint, OAuth, user identity, permission enforcement, folder scope enforcement, request validation, audit logging) → reMarkable service layer (cloud authentication; browse, read, render, export, upload, mkdir, move, rename, trash) → reMarkable Cloud → the user's tablet(s).

## 3. Core architectural principle *(superseded in part)*
Cloudflare is the authenticated MCP gateway. The hosted version uses only the reMarkable Cloud transport — not USB, SSH, or the local desktop cache. *(Original text proposed porting the transport to a Worker; `spec-cloudflare.md` keeps the Python server whole in a Container instead.)*

## 4. Public MCP endpoint
One remote MCP endpoint (for example `https://<host>/mcp`) using MCP Streamable HTTP, OAuth authentication, per-user authorization context, TLS through Cloudflare, no anonymous access. All supported MCP clients connect to the same endpoint.

## 5. User identity
MCP authentication identifies the human user: person → OAuth → MCP identity → that person's linked reMarkable account. A model must never be able to specify another user's identity in a tool call (no `remarkable_browse(user="...")`). Required design: authenticated MCP session → resolve current user → resolve that user's reMarkable credential → execute. The user/account relationship is enforced server-side.

## 6. reMarkable account connection
MCP identity and reMarkable identity are separate; the server maps one to the other. Suggested `users` table: user_id, email, display_name, remarkable_account_id, remarkable_credential_ref, status, created_at, updated_at. The actual reMarkable credential is not stored in this table.

## 7. Credential storage
Each reMarkable credential is stored separately and securely. Prototype: a Worker secret is acceptable for one user. Production/multi-user: encrypted per-user credential records; only a reference in the user table; a running request can retrieve only the current user's credential. Never expose credentials to MCP clients; never return them in tool results; never log them raw; never put registration codes or credentials into model-visible text; support revocation/disconnection and re-registration without rebuilding the service.

## 8. Cloudflare components *(superseded in part)*
Worker: endpoint, OAuth, session identity, permission checks, tool routing, validation, folder scoping, audit events. D1: users, remarkable_connections, permission_sets, folder_scopes, audit_events, connection_status. R2: temporary/cached binary artifacts (rendered pages, temporary PDFs, exports, cached archives, previews); never the authoritative store. KV: lightweight caches only. Durable Objects: not in v1 unless needed. *(Phase 1 in `spec-cloudflare.md` uses KV for OAuth state only; D1 and R2 are phase 2.)*

## 9. MCP tool surface
Read tools: remarkable_status, remarkable_browse, remarkable_recent, remarkable_search, remarkable_read, remarkable_image, remarkable_export. Write tools: remarkable_upload, remarkable_mkdir, remarkable_move, remarkable_rename, remarkable_delete. `remarkable_delete` means move to reMarkable Trash; permanent deletion is not exposed.

## 10. Permission model
READ: status, browse, recent, search, read, image, export. CREATE: upload, mkdir. MANAGE: move, rename, trash. OAuth scopes `remarkable.read`, `remarkable.create`, `remarkable.manage` — or, simpler, `remarkable.read` and `remarkable.write`. Server-side enforcement is required; never rely on model instructions alone.

## 11. Folder scope enforcement
An optional server-side root folder scope (for example `/`, `/Shared`, `/Team`). A scoped connection must be incapable of accessing paths outside its root, even when asked. Enforce by document/folder IDs and ancestry, not only string comparison.

## 12. Scoping use cases
A read-only project assistant scoped to one folder with `remarkable.read`; a planning assistant scoped to a shared folder with read + create + manage; a personal assistant with `/` and `remarkable.read`.

## 13. reMarkable service layer
All reMarkable-specific behavior is behind one internal interface: getStatus, listFolder, getDocument, readDocument, renderPage, exportDocument, uploadDocument, createFolder, renameItem, moveItem, trashItem. The service resolves the current user, the linked credential, the allowed root and the permissions before any operation. *(In phase 1 the Python server is this layer; the gateway resolves the user and forwards.)*

## 14. Port only the cloud transport *(superseded)*
Do not carry USB, SSH, local desktop-cache mode, Developer Mode behaviors, xochitl refresh logic or native SSH notebook authoring into the hosted service.

## 15. Reference implementation
The local Python environment stays intact and is used to validate the hosted implementation: for a given operation, hosted behavior should match the local reference before being considered production-ready.

## 16. Verified baseline (2026-11-03)
The local cloud prototype verified: cloud transport, authenticated, writes enabled; browse; read and render of a test folder; mkdir; a one-page PDF upload; verification of the uploaded PDF's text and image; rename; move; trash; a fresh browse after each mutation. No native notebook editing, no Developer Mode or SSH, no Cloudflare deployment.

## 17. Rendering and parsing caution
The `rmscene` parser has logged newer-format parsing warnings on at least one native notebook; rendering succeeded, but native element-level fidelity is not fully verified. Do not claim perfect native notebook parsing; keep a fallback rendering path; treat parser warnings as diagnostics; avoid destructive rewrites of native notebooks.

## 18. Native notebook editing
Explicitly outside v1. Cloud capabilities today: upload, mkdir, move, rename, trash. Possible future capabilities (create notebook, add page, append strokes, modify archive) go behind the service interface so the public endpoint and authentication do not change.

## 19. Account management UI
A lightweight page per user: connection status, permissions, folder access, reconnect/disconnect. Registration codes are entered here, never inside MCP conversations.

## 20. Safety requirements
Least privilege by default; no cross-user credential access; no secrets to the model; no raw credentials in logs; no arbitrary permanent deletion; validate document IDs before mutation; verify state after writes; scope access server-side; log write operations; reject requests outside granted permissions or outside the folder scope. Audit record fields: timestamp, user_id, tool, document_id, document_name, old_parent, new_parent, result, client, request_id. Do not log document content.

## 21. Write safety
Rename: item exists, belongs to the current user, inside scope, valid target name. Move: source and destination exist, both inside scope, no escape from the root. Trash: exact document UUID, current path, user permission, inside scope; then move to Trash; never permanent deletion. Upload: supported file type, size limit, destination folder inside scope.

## 22. Concurrency
No concurrent mutations against the same reMarkable account until behavior is well understood; serialize writes per user/account; reads may run concurrently.

## 23. Caching policy
reMarkable Cloud is authoritative; caches are disposable. Library metadata: short TTL. Rendered page: medium. Export: short. Write results: never trusted from cache. After every mutation: invalidate, re-read from the cloud, verify.

## 24. Failure handling
Never blindly retry an ambiguous write failure. Inspect fresh cloud state, resolve the target by UUID, determine whether the mutation already happened, retry only if it did not. Prefer idempotency.

## 25–27. Deployment scope, authentication scope, phases *(superseded)*
See `spec-cloudflare.md`. The lookup remains authenticated_user_id → remarkable_connections → credential_ref, never a single global token as the application model.

## 28. Success criteria for v1
ChatGPT and Claude connect to the remote endpoint; the authenticated user resolves to the correct reMarkable account; status, browse, read, image, upload, mkdir, rename, move, trash work remotely; no local process, USB, Developer Mode or SSH required; writes confirmed by a fresh cloud read; Trash recoverable; credentials never appear in MCP output; a second user can be added without redesign.

## 29. Non-goals for v1
Developer Mode, SSH, USB, native notebook editing, permanent deletion, tablet firmware modification, automatic background rewriting of notebooks, real-time collaboration.

## 30–31. Technology direction and guiding principle *(30 superseded)*
Treat the system as a multi-user authenticated MCP gateway plus isolated reMarkable cloud accounts, rather than one personal script exposed to the internet: safe and maintainable for one user now and more later.
