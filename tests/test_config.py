import pytest
import web3

from degenbot.config import get_web3, set_web3, web3_connection_manager
from degenbot.exceptions import DegenbotError


def test_disconnected_web3():
    w3 = web3.Web3(web3.HTTPProvider("https://google.com"))
    with pytest.raises(DegenbotError, match="Web3 instance is not connected."):
        set_web3(w3)

    with pytest.raises(DegenbotError, match="Web3 instance is not connected."):
        web3_connection_manager.register_web3(w3)


def test_legacy_interface(ethereum_archive_node_web3: web3.Web3):
    with pytest.raises(DegenbotError, match="A default Web3 instance has not been registered."):
        get_web3()

    set_web3(ethereum_archive_node_web3)
    assert get_web3() is ethereum_archive_node_web3


def test_connection_manager(ethereum_archive_node_web3: web3.Web3):
    set_web3(ethereum_archive_node_web3)
    assert (
        web3_connection_manager.get_web3(ethereum_archive_node_web3.eth.chain_id)
        is ethereum_archive_node_web3
    )
