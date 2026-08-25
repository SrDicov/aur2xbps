{
  description = "aur2xbps — transpiler AUR → Nix → XBPS";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-24.11";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachSystem [ "x86_64-linux" ] (system:
      let
        pkgs = import nixpkgs { inherit system; };
        python = pkgs.python3.withPackages (ps: with ps; [ pytest pytest-timeout httpx pyyaml ]);
      in {
        devShells.default = pkgs.mkShell {
          buildInputs = with pkgs; [
            python
            patchelf
            binutils
            pkg-config
            nix
            git
            curl
            jq
            zstd
          ];
          shellHook = ''
            export SOURCE_DATE_EPOCH=0
            echo "aur2xbps devShell — nix ${pkgs.nix.version}, patchelf ${pkgs.patchelf.version}"
          '';
        };
        packages.aur2xbps = python.pkgs.buildPythonPackage {
          pname = "aur2xbps";
          version = "0.2.0";
          src = ./.;
          pyproject = true;
          build-system = with pkgs.python3Packages; [ setuptools wheel ];
          propagatedBuildInputs = with pkgs.python3Packages; [ httpx pyyaml ];
          doCheck = false;
        };
        checks.lint-patchelf = pkgs.runCommand "lint-patchelf" { src = ./.; } ''
          cd $src
          ${python}/bin/python -m src.nix.patchelf > /dev/null
          touch $out
        '';
      }
    );
}
