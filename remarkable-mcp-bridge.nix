{ config, lib, pkgs, ... }:

let
  cfg = config.services.remarkable-mcp;
  loopbackHosts = [ "127.0.0.1" "::1" "localhost" ];
in
{
  options.services.remarkable-mcp = {
    enable = lib.mkEnableOption
      "the reMarkable MCP server over Streamable HTTP for a local OpenWebUI instance";

    user = lib.mkOption {
      type = lib.types.str;
      default = "remarkable-mcp";
      description = "System user that runs the bridge and owns its reMarkable credentials.";
    };

    group = lib.mkOption {
      type = lib.types.str;
      default = "remarkable-mcp";
      description = "System group for the bridge service.";
    };

    createUser = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Whether this module should create the configured service user and group.";
    };

    command = lib.mkOption {
      type = lib.types.str;
      default = "/opt/remarkable-mcp/.venv/bin/remarkable-mcp";
      description = "Path to the installed remarkable-mcp executable.";
    };

    host = lib.mkOption {
      type = lib.types.str;
      default = "127.0.0.1";
      description = ''
        Streamable HTTP bind address. Keep this on loopback. A reverse proxy
        must authenticate requests, rewrite Host to the loopback upstream, and
        clear Origin; see the README.
      '';
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 8000;
      description = "TCP port for the Streamable HTTP endpoint.";
    };

    home = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/remarkable-mcp";
      description = "Home directory containing the service user's reMarkable credentials.";
    };

    extraEnvironment = lib.mkOption {
      type = lib.types.attrsOf lib.types.str;
      default = { };
      description = "Additional environment variables for remarkable-mcp.";
    };
  };

  config = lib.mkIf cfg.enable {
    warnings = lib.optional (!(builtins.elem cfg.host loopbackHosts)) ''
      services.remarkable-mcp.host is non-loopback. Streamable HTTP has no
      authentication. Prefer the loopback default; an authenticated reverse
      proxy must also rewrite Host to 127.0.0.1:${toString cfg.port} and clear
      Origin to satisfy FastMCP's DNS-rebinding protection.
    '';

    users.groups = lib.mkIf cfg.createUser {
      ${cfg.group} = { };
    };

    users.users = lib.mkIf cfg.createUser {
      ${cfg.user} = {
        isSystemUser = true;
        group = cfg.group;
        home = cfg.home;
        createHome = true;
      };
    };

    systemd.services.remarkable-mcp-bridge = {
      description = "reMarkable MCP Streamable HTTP bridge";
      wantedBy = [ "multi-user.target" ];
      after = [ "network.target" ];
      environment = cfg.extraEnvironment // {
        HOME = toString cfg.home;
        PATH = "${pkgs.tesseract}/bin:/run/current-system/sw/bin";
      };
      serviceConfig = {
        Type = "simple";
        User = cfg.user;
        Group = cfg.group;
        ExecStart = "${cfg.command} --http --host ${lib.escapeShellArg cfg.host} --port ${toString cfg.port}";
        Restart = "on-failure";
        RestartSec = "5s";
        NoNewPrivileges = true;
        PrivateTmp = true;
      };
    };
  };
}
