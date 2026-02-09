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
      in
      {
        devShells.default = pkgs.mkShell {
          buildInputs = with pkgs; [
            # Node.js and npm
            nodejs_24
            # npm is included with nodejs

            # Encryption tools
            age
            sops

            # Python
            python313
            uv

            # Usage CLI (mise plugin)
            usage
          ];

          shellHook = ''
            # Auto-sync Python dependencies
            uv sync --python $(command -v python)
            export PATH="$PWD/.venv/bin:$PATH"
          '';
        };
      }
    );
}
