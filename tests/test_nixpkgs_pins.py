# SPDX-License-Identifier: GPL-3.0-or-later
"""Fase 0 campaña: alias AUR2XBPS_ROOT, sección [nixpkgs_pins] y
resolve_nixos_ref (pin por-paquete > env > global)."""
from pathlib import Path

import pytest

from src.common.config import get_config, load_config
from src.aur.parser import parse_srcinfo


@pytest.fixture()
def isolated_cfg(tmp_path, monkeypatch):
    """Config aislada: TOML inexistente en tmp + restauración del singleton."""
    monkeypatch.setenv("AUR2XBPS_CONFIG", str(tmp_path / "cfg.toml"))
    yield tmp_path / "cfg.toml"
    get_config(reload=True)


# ------------------------------------------------------------ alias ROOT
def test_root_env_alias_define_data_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("AUR2XBPS_DATA_DIR", raising=False)
    monkeypatch.setenv("AUR2XBPS_ROOT", str(tmp_path / "ws"))
    cfg = load_config(tmp_path / "no-existe.toml")
    assert cfg.data_dir == Path(tmp_path / "ws")
    assert cfg.repo_x86_64 == cfg.data_dir / "repo" / cfg.arch


def test_root_env_gana_sobre_toml(tmp_path, monkeypatch):
    toml = tmp_path / "cfg.toml"
    toml.write_text('[paths]\ndata_dir = "/tmp/otro"\n', encoding="utf-8")
    monkeypatch.delenv("AUR2XBPS_DATA_DIR", raising=False)
    monkeypatch.setenv("AUR2XBPS_ROOT", str(tmp_path / "gana"))
    cfg = load_config(toml)
    assert cfg.data_dir == Path(tmp_path / "gana")


# --------------------------------------------------------- nixpkgs_pins
def test_toml_seccion_nixpkgs_pins(tmp_path):
    toml = tmp_path / "cfg.toml"
    toml.write_text(
        '[nixpkgs_pins]\nparu = "github:NixOS/nixpkgs/abc123"\n'
        'yay-bin = "github:NixOS/nixpkgs/def456"\n',
        encoding="utf-8")
    cfg = load_config(toml)
    assert cfg.nixpkgs_pins == {
        "paru": "github:NixOS/nixpkgs/abc123",
        "yay-bin": "github:NixOS/nixpkgs/def456",
    }


def test_pins_sin_seccion_queda_vacio(tmp_path):
    cfg = load_config(tmp_path / "no-existe.toml")
    assert cfg.nixpkgs_pins == {}


# ------------------------------------------------------ resolve_nixos_ref
def _si(base="paru"):
    return parse_srcinfo(f"pkgbase = {base}\npkgname = {base}\n")


def test_pin_pkgbase_prioriza(isolated_cfg, monkeypatch):
    isolated_cfg.write_text(
        '[nixpkgs_pins]\nparu = "github:NixOS/nixpkgs/pinrev"\n',
        encoding="utf-8")
    monkeypatch.setenv("AUR2XBPS_NIXOS_REF", "github:NixOS/nixpkgs/envref")
    get_config(reload=True)
    from src.nix.generator import resolve_nixos_ref
    assert resolve_nixos_ref(_si("paru")) == "github:NixOS/nixpkgs/pinrev"


def test_otro_paquete_no_hereda_pin(isolated_cfg, monkeypatch):
    isolated_cfg.write_text(
        '[nixpkgs_pins]\nparu = "github:NixOS/nixpkgs/pinrev"\n',
        encoding="utf-8")
    monkeypatch.delenv("AUR2XBPS_NIXOS_REF", raising=False)
    get_config(reload=True)
    from src.nix.generator import NIXOS_REF, resolve_nixos_ref
    assert resolve_nixos_ref(_si("yay")) == NIXOS_REF


def test_env_fallback_y_default(isolated_cfg, monkeypatch):
    from src.nix.generator import NIXOS_REF, resolve_nixos_ref

    monkeypatch.delenv("AUR2XBPS_NIXOS_REF", raising=False)
    get_config(reload=True)
    assert resolve_nixos_ref(_si()) == NIXOS_REF

    monkeypatch.setenv("AUR2XBPS_NIXOS_REF", "github:NixOS/nixpkgs/envref")
    assert resolve_nixos_ref(_si()) == "github:NixOS/nixpkgs/envref"


def test_generate_flake_usa_ref_resuelto(tmp_path, isolated_cfg):
    """El header del flake lleva el ref pineado (sin lock ni red)."""
    isolated_cfg.write_text(
        '[nixpkgs_pins]\nparu = "github:NixOS/nixpkgs/pinrev"\n',
        encoding="utf-8")
    get_config(reload=True)
    from src.nix.generator import generate_flake
    flake = generate_flake(_si("paru"), tmp_path,
                           nixos_ref="github:NixOS/nixpkgs/pinrev")
    txt = flake.read_text()
    assert 'nixpkgs.url = "github:NixOS/nixpkgs/pinrev"' in txt
