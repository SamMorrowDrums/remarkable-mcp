let
  evaluated = import <nixpkgs/nixos/lib/eval-config.nix> {
    modules = [
      ../remarkable-mcp-bridge.nix
      {
        services.remarkable-mcp.enable = true;
        system.stateVersion = "24.11";
      }
    ];
  };
  config = evaluated.config;
  service = config.systemd.services.remarkable-mcp-bridge;
  unitText = config.systemd.units."remarkable-mcp-bridge.service".text;
in
assert builtins.elem evaluated.pkgs.tesseract service.path;
assert service.environment.HOME == "/var/lib/remarkable-mcp";
assert service.serviceConfig.NoNewPrivileges;
assert service.serviceConfig.PrivateTmp;
assert service.serviceConfig.ExecStart
  == "/opt/remarkable-mcp/.venv/bin/remarkable-mcp --http --host 127.0.0.1 --port 8000";
assert builtins.isString unitText;
true
