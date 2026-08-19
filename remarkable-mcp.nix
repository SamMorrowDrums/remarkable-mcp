# remarkable-mcp built with nixpkgs' Python builders (fully offline).
#
# Build:
#   nix build .#remarkable-mcp
#   ./result/bin/remarkable-mcp --help
#
# Usage in config:
#   {pkgs, ...}:
#     let rm = pkgs.callPackage ./remarkable-mcp.nix {};
#     in { services.remarkable-mcp.package = rm; }

{ pkgs ? import <nixpkgs> {} }:

let
  src = ./.;
  py = pkgs.python3.pkgs;

  # mcp 2.x requires mcp-types, which nixpkgs doesn't ship, and builds via the
  # uv-dynamic-versioning hatch plugin, so install the locked wheels directly.
  mcp-types = py.buildPythonPackage {
    pname = "mcp-types";
    version = "2.0.0";
    format = "wheel";
    src = pkgs.fetchurl {
      url = "https://files.pythonhosted.org/packages/f5/4c/c78d78c3d52b0ac594ad7cc8ef5972adfe070e3597a8a4c6ce0cd39196ea/mcp_types-2.0.0-py3-none-any.whl";
      sha256 = "1c4f3dnrl0zizs1bvh5w27jlvqs8b6rf2acmnxlgb5r7rabyfbbb";
    };
    dependencies = [ py.pydantic py.typing-extensions ];
  };

  mcp = py.buildPythonPackage {
    pname = "mcp";
    version = "2.0.0";
    format = "wheel";
    src = pkgs.fetchurl {
      url = "https://files.pythonhosted.org/packages/67/72/7d7897418912c1d12e87556630dfb7bf0eac71160e9bef8b447960804ee3/mcp-2.0.0-py3-none-any.whl";
      sha256 = "1mj94dcs6z0x0ni24gpiqwn85wir5bcfamb3flfqqyrc5mfwgd0w";
    };
    dependencies = [
      py.anyio
      py.httpx2
      py.jsonschema
      mcp-types
      py.opentelemetry-api
      py.pydantic
      py.pyjwt
      py.cryptography # pyjwt[crypto]
      py.python-multipart
      py.sse-starlette
      py.starlette
      py.typing-extensions
      py.typing-inspection
      py.uvicorn
    ];
  };

  # Build the project with hatchling; all dependencies come from nixpkgs plus
  # the wheel-based mcp/mcp-types above.
  app = py.buildPythonApplication {
    pname = "remarkable-mcp";
    version = "0.1.0";
    pyproject = true;
    inherit src;
    nativeBuildInputs = [ py.hatchling ];
    propagatedBuildInputs = [
      mcp
      py.rmscene
      py.pytesseract
      py.pillow
      py.requests
      py.pymupdf
      py.ebooklib
      py.beautifulsoup4
      py.markdown-it-py
    ];
  };

  # Wrapper that ensures tesseract is in PATH when running remarkable-mcp.
  wrapper = pkgs.writeShellScriptBin "remarkable-mcp" ''
    PATH="${pkgs.tesseract5}/bin:${pkgs.libjpeg_turbo}/bin:$PATH"
    exec "${app}/bin/remarkable-mcp" "$@"
  '';

in
{
  # Direct package only (no wrapper)
  venv = app;

  # Full package with tesseract + libjpeg in PATH via wrapper
  package = pkgs.buildEnv {
    name = "remarkable-mcp";
    paths = [ wrapper pkgs.tesseract5 pkgs.libjpeg_turbo ];
    pathsToLink = [ "/bin" ];
  };

  # Dev shell
  dev = pkgs.mkShell {
    inputsFrom = [ app ];
    nativeBuildInputs = [ pkgs.uv pkgs.tesseract5 ];
  };
}
