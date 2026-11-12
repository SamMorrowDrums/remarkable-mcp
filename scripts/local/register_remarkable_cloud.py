"""Exchange a code received on stdin using the installed MCP registration API.

The PowerShell launcher creates an empty, access-restricted ~/.rmapi first.
Neither the code nor the resulting credential is printed or passed on argv.
"""
import contextlib
import io
import logging
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))


def main():
    token_file = pathlib.Path.home() / ".rmapi"
    code = sys.stdin.readline().strip()
    if not re.fullmatch(r"[A-Za-z0-9]{8}", code):
        print("Registration refused: expected an eight-character one-time code.")
        return 2
    if not token_file.is_file() or token_file.stat().st_size != 0:
        print("Registration refused: protected empty credential file is required.")
        return 2
    logging.disable(logging.CRITICAL)
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            from remarkable_mcp.api import register_and_get_token

            credential = register_and_get_token(code)
            if not credential or token_file.stat().st_size == 0:
                raise RuntimeError("Credential was not saved")
        code = credential = None
        print("Registration succeeded; the credential was saved privately to ~/.rmapi.")
        return 0
    except Exception as exc:
        # Do not print exception text: dependency errors may contain credentials.
        print(f"Registration failed ({type(exc).__name__}); obtain a fresh code and retry.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
