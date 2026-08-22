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
        python = pkgs.python3.withPackages (ps: with ps; [ pytest pyyaml requests ]);
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
          version = "0.1.0";
          src = ./.;
          propagatedBuildInputs = with pkgs.python3Packages; [ requests pyyaml ];
          doCheck = false;
        };
        checks.lint-patchelf = pkgs.runCommand "lint-patchelf" {} ''
          ${python}/bin/python -m src.nix.patchelf --help > /dev/null
          touch $out
        '';
      }
    );
}
