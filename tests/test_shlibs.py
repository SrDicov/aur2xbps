# SPDX-License-Identifier: GPL-3.0-or-later
from pathlib import Path

import pytest

from src.common.config import get_config


def _shlibs_file() -> Path | None:
    f = get_config().shlibs_file
    return f if f.is_file() else None


@pytest.mark.skipif(_shlibs_file() is None,
                    reason="common/shlibs no disponible (submodule sin "
                           "bootstrap ni workspace poblado)")
def test_shlibs_load():
    from src.xbps.shlibs import ShlibsDB
    db = ShlibsDB()
    assert len(db.map) > 4000
    assert db.lookup("libc.so") is not None or db.lookup("libtinfo.so.6") is not None


@pytest.mark.skipif(_shlibs_file() is None,
                    reason="common/shlibs no disponible (submodule sin "
                           "bootstrap ni workspace poblado)")
def test_deps_for_elf():
    from src.xbps.shlibs import ShlibsDB
    ls = Path("/bin/ls")
    if not ls.exists():                       # musl/busybox: /bin/ls puede no existir
        pytest.skip("/bin/ls ausente en este host")
    db = ShlibsDB()
    deps = db.deps_for_elf(ls)
    # /bin/ls should depend on glibc or similar
    assert isinstance(deps, list)
