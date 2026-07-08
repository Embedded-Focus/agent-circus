{
  pkgs,
  lib,
  config,
  ...
}:
{
  packages = [
    pkgs.sops
    pkgs.age
  ];

  # https://devenv.sh/languages/
  languages.python = {
    enable = true;
    uv.enable = true;
  };

  # See full reference at https://devenv.sh/reference/options/
}
