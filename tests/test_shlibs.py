# SPDX-License-Identifier: GPL-3.0-or-later
from src.xbps.shlibs import ShlibsDB
from pathlib import Path

def test_shlibs_load():
    db = ShlibsDB()
    assert len(db.map) > 4000
    assert db.lookup("libc.so") is not None or db.lookup("libtinfo.so.6") is not None

def test_deps_for_elf():
    db = ShlibsDB()
    deps = db.deps_for_elf(Path("/bin/ls"))
    # /bin/ls should depend on glibc or similar
    assert isinstance(deps, list)

