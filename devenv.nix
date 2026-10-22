{
  pkgs,
  lib,
  config,
  inputs,
  ...
}:
let
  # Import plain nixpkgs-unstable (as an extra input) just for packages that
  # cachix/devenv-nixpkgs has not synced yet, e.g. uv. Remove once devenv's
  # rolling nixpkgs carries a sufficiently new uv.
  uvs = import inputs.nixpkgs-uvs { system = pkgs.stdenv.hostPlatform.system; };
in
{
  packages = [
    pkgs.git
    pkgs.git-absorb
    pkgs.sops
    pkgs.age
    pkgs.yq-go
    uvs.uv
  ];

  # https://devenv.sh/languages/
  languages.python = {
    enable = true;
    version = "3.14.8";
    venv.enable = true;
    uv = {
      enable = true;
      package = uvs.uv;
      sync.enable = true;
    };
  };

  dotenv.disableHint = true;

  # See full reference at https://devenv.sh/reference/options/
}
