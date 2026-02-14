{
  description = "Development environment for agent-circus";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs {
          inherit system;
        };
        libPath = pkgs.lib.makeLibraryPath [
          pkgs.stdenv.cc.cc.lib   # provides libstdc++.so.6 (and libgcc_s)
        ];
      in
      {
        devShells.default = pkgs.mkShell {
          buildInputs = with pkgs; [
            # Encryption tools
            age
            sops

            # Python
            python313
            uv

            # Usage CLI (mise plugin)
            usage

            stdenv.cc.cc.lib
          ];

          shellHook = ''
            export LD_LIBRARY_PATH=${libPath}:''${LD_LIBRARY_PATH:-}

            # Auto-sync Python dependencies
            uv sync --python $(command -v python)
            export PATH="$PWD/.venv/bin:$PATH"
          '';
        };
      }
    );
}
