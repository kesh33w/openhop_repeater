"""Temporary local sockets only; never modem or external network I/O."""
import os
import socket
import stat
import pytest
from openhop_core.rf_fabric.fabric import RFFabric
from repeater.bot_rf_receipts import RawReceiptServer


def server(path, group=False):
    binding={'receivers':[]}
    return RawReceiptServer(fabric=RFFabric(), binding=binding, secret=b'x'*32,
        socket_path=str(path), binding_provider=lambda:binding, socket_group_access=group)


@pytest.mark.asyncio
async def test_owned_stale_socket_recovers_and_lock_excludes_second(tmp_path):
    path=tmp_path/'proof.sock'
    old=socket.socket(socket.AF_UNIX);old.bind(str(path));old.close()
    first=server(path);second=server(path)
    try:
        await first.start()
        assert stat.S_IMODE(path.stat().st_mode)==0o600
        with pytest.raises(BlockingIOError): await second.start()
        assert path.exists()
    finally: await first.close()
    assert not path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['file','symlink','active'])
async def test_refuses_unsafe_existing_path(tmp_path,kind):
    path=tmp_path/'proof.sock'; listener=None
    if kind=='file': path.write_text('preserve')
    elif kind=='symlink': path.symlink_to(tmp_path/'missing')
    else:
        listener=socket.socket(socket.AF_UNIX);listener.bind(str(path));listener.listen(1)
    try:
        with pytest.raises(ValueError): await server(path).start()
        assert path.exists() or path.is_symlink()
    finally:
        if listener:listener.close()


@pytest.mark.asyncio
async def test_optin_group_access_requires_exact_parent_mode(tmp_path):
    path=tmp_path/'proof.sock'
    with pytest.raises(ValueError): await server(path,True).start()
    tmp_path.chmod(0o710)
    instance=server(path,True)
    try:
        await instance.start()
        assert stat.S_IMODE(path.stat().st_mode)==0o660
        assert path.stat().st_gid==tmp_path.stat().st_gid
    finally: await instance.close()
