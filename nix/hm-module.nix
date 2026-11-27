self: {
  config,
  pkgs,
  lib,
  ...
}: let
  inherit (pkgs.stdenv.hostPlatform) system;

  cli-default = self.inputs.helysium-cli.packages.${system}.default;
  shell-default = self.packages.${system}.with-cli;

  cfg = config.programs.helysium;
in {
  imports = [
    (lib.mkRenamedOptionModule ["programs" "helysium" "environment"] ["programs" "helysium" "systemd" "environment"])
  ];
  options = with lib; {
    programs.helysium = {
      enable = mkEnableOption "Enable HelysIum shell";
      package = mkOption {
        type = types.package;
        default = shell-default;
        description = "The package of HelysIum shell";
      };
      systemd = {
        enable = mkOption {
          type = types.bool;
          default = true;
          description = "Enable the systemd service for HelysIum shell";
        };
        target = mkOption {
          type = types.str;
          description = ''
            The systemd target that will automatically start the HelysIum shell.
          '';
          default = config.wayland.systemd.target;
        };
        environment = mkOption {
          type = types.listOf types.str;
          description = "Extra Environment variables to pass to the HelysIum shell systemd service.";
          default = [];
          example = [
            "QT_QPA_PLATFORMTHEME=gtk3"
          ];
        };
      };
      settings = mkOption {
        type = types.attrsOf types.anything;
        default = {};
        description = "HelysIum shell settings";
      };
      extraConfig = mkOption {
        type = types.str;
        default = "";
        description = "HelysIum shell extra configs written to shell.json";
      };
      cli = {
        enable = mkEnableOption "Enable HelysIum CLI";
        package = mkOption {
          type = types.package;
          default = cli-default;
          description = "The package of HelysIum CLI"; # Doesn't override the shell's CLI, only change from home.packages
        };
        settings = mkOption {
          type = types.attrsOf types.anything;
          default = {};
          description = "HelysIum CLI settings";
        };
        extraConfig = mkOption {
          type = types.str;
          default = "";
          description = "HelysIum CLI extra configs written to cli.json";
        };
      };
    };
  };

  config = let
    cli = cfg.cli.package;
    shell = cfg.package;
  in
    lib.mkIf cfg.enable {
      systemd.user.services.helysium = lib.mkIf cfg.systemd.enable {
        Unit = {
          Description = "HelysIum Shell Service";
          After = [cfg.systemd.target];
          PartOf = [cfg.systemd.target];
          X-Restart-Triggers = lib.mkIf (cfg.settings != {}) [
            "${config.xdg.configFile."helysium/shell.json".source}"
          ];
        };

        Service = {
          Type = "exec";
          ExecStart = "${shell}/bin/helysium-shell";
          Restart = "on-failure";
          RestartSec = "5s";
          TimeoutStopSec = "5s";
          Environment =
            [
              "QT_QPA_PLATFORM=wayland"
            ]
            ++ cfg.systemd.environment;

          Slice = "session.slice";
        };

        Install = {
          WantedBy = [cfg.systemd.target];
        };
      };

      xdg.configFile = let
        mkConfig = c:
          lib.pipe (
            if c.extraConfig != ""
            then c.extraConfig
            else "{}"
          ) [
            builtins.fromJSON
            (lib.recursiveUpdate c.settings)
            builtins.toJSON
          ];
        shouldGenerate = c: c.extraConfig != "" || c.settings != {};
      in {
        "helysium/shell.json" = lib.mkIf (shouldGenerate cfg) {
          text = mkConfig cfg;
        };
        "helysium/cli.json" = lib.mkIf (shouldGenerate cfg.cli) {
          text = mkConfig cfg.cli;
        };
      };

      home.packages = [shell] ++ lib.optional cfg.cli.enable cli;
    };
}
