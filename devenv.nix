{
  pkgs,
  lib,
  config,
  ...
}:
{
  packages = [
    pkgs.git
    pkgs.git-absorb
    pkgs.sops
    pkgs.age
  ];

  # https://devenv.sh/languages/
  languages.python = {
    enable = true;
    version = "3.14";
    venv.enable = true;
    uv.enable = true;
    uv.sync.enable = true;
  };

  dotenv.disableHint = true;

  # See full reference at https://devenv.sh/reference/options/
}
