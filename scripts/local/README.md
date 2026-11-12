# Running the server locally on Windows

Urban Forest's helpers for running this server on a Windows computer, before or alongside the Cloudflare deployment described in `docs/urban-forest/spec-cloudflare.md`. All paths are relative to the repository root; nothing here contains a token, a code or anything from a tablet.

## One-time setup

1. Create the Python environment at the repository root with [uv](https://docs.astral.sh/uv/): `uv sync` (the launchers expect `.venv\Scripts\python.exe` there).
2. Link the server to your reMarkable account: run `Register-RemarkableCloud.ps1` in a normal PowerShell window. It opens a masked prompt for the eight-character desktop-client code from https://my.remarkable.com/device/desktop/connect, passes the code to `register_remarkable_cloud.py` over standard input (never as an argument), and stores the resulting token at `%USERPROFILE%\.rmapi` with access restricted to you. It never overwrites an existing token. Cloud mode needs a reMarkable Connect subscription.

## Cloud mode (the normal case)

- `Start-RemarkableCloud.ps1` — starts `python -m remarkable_mcp.cli --http --host 127.0.0.1 --port 8000` with USB, SSH and local-directory modes off, library changes on (the server's default), cloud fallback off, delete confirmations kept, Tesseract OCR, and a cache in `scripts\local\.cloud-cache`. It listens only on the computer's own loopback address, with no sign-in — any process on the computer can reach it. It refuses to start while a tunnel runtime or another port-8000 listener is up. Logs go to `scripts\local\logs\cloud-<timestamp>\`; the process details to `scripts\local\cloud-server-state.json`.
- `Stop-RemarkableCloud.ps1` — stops the process recorded in that state file, after checking it is the expected Python.
- `verify_cloud_mcp.py --document "/<folder>/<document>"` — a read check: status, browse, and a read plus render of the document you name. Add `--writes` for the library-change check: it creates a uniquely named test folder, pushes a one-page disposable PDF into it, renames, moves, and trashes the PDF and the folders, with a fresh browse after every step and a comparison of the root listing before and after. Reports land in `scripts\local\cloud-verification\<timestamp>\` (ignored by git — they contain names from your library). A failed step is never retried; read the report first.

Run the verifier with the repository's Python: `.\.venv\Scripts\python.exe .\scripts\local\verify_cloud_mcp.py --document "..."`.

## USB mode (fallback, read-only)

- `Start-RemarkableMcp.ps1` / `Stop-RemarkableMcp.ps1` — the same server over the tablet's USB web interface (`http://10.11.99.1`), read-only, no cloud fallback. Needs the tablet connected by cable with its USB web interface on. If a Tesseract build sits in `scripts\local\.tools\tesseract\`, it is put on the path.
- `verify_usb_mcp.py [--document ...]` — status and browse, optionally a read and render.

## Claude desktop app

`claude-desktop-remarkable.example.json` is an entry for the Claude desktop app's `claude_desktop_config.json` (Settings → Developer → Edit Config): the app starts the server itself over standard input and output, with the same settings as the cloud launcher. Replace `<REPO>` with the repository's full path. Library changes are on in this example; add `--read-only` to `args` for a read-only server.

## What never goes in this repository

The `.rmapi` token file, registration codes, state files, logs, caches and verification reports. The `.gitignore` at the root covers them; keep it that way.
