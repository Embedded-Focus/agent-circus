{
  description = "Development environment for agent-circus";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = { nixpkgs, ... }:
    let
      systems = [
        "x86_64-linux"
      ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
    in
    {
      devShells = forAllSystems (system:
        let
          pkgs = import nixpkgs {
            inherit system;
          };
          libPath = pkgs.lib.makeLibraryPath [
            pkgs.stdenv.cc.cc.lib   # provides libstdc++.so.6 (and libgcc_s)
          ];
        in
        {
          default = pkgs.mkShell {
            buildInputs = with pkgs; [
              # Encryption tools
              age
              sops

              # Python
              uv

              stdenv.cc.cc.lib
            ];

            shellHook = ''
              export LD_LIBRARY_PATH=${libPath}:''${LD_LIBRARY_PATH:-}
            '';
          };
        });
    };
}
