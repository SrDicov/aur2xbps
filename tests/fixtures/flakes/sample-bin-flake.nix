{ pkgs ? import <nixpkgs> {} }:
let
  name = "sample-bin";
  src = pkgs.fetchurl {
    url = "https://example.com/sample-1.0.tar.gz";
    sha256 = "0000000000000000000000000000000000000000000000000000000000000000";
  };
in pkgs.stdenv.mkDerivation {
  inherit name;
  inherit src;
  nativeBuildInputs = [ pkgs.autoPatchelfHook ];
  installPhase = ''
    runHook preInstall
    mkdir -p $out/bin
    cp sample $out/bin/
    patchelf --set-rpath "$ORIGIN:/usr/lib:/usr/lib64" $out/bin/sample
    chmod +w $out/bin/sample
    patchelf --set-interpreter /lib64/ld-linux-x86-64.so.2 $out/bin/sample
    chmod -w $out/bin/sample
    runHook postInstall
  '';
}
