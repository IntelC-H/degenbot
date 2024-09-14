__all__ = (
    "get_web3",
    "set_web3",
    "web3_connection_manager",
)

import web3

from .exceptions import DegenbotError


class Web3ConnectionManager:
    def __init__(self) -> None:
        self.connections: dict[int, web3.Web3] = dict()
        self.default_chain_id: int | None = None

    def get_web3(self, chain_id: int) -> web3.Web3:
        try:
            return self.connections[chain_id]
        except KeyError:
            raise DegenbotError("Chain ID does not have a registered Web3 instance.") from None

    def register_web3(self, w3: web3.Web3) -> None:
        if w3.is_connected() is False:
            raise DegenbotError("Web3 instance is not connected.")
        self.connections[w3.eth.chain_id] = w3

    def set_default_chain(self, chain_id: int) -> None:
        self.default_chain_id = chain_id


def get_web3() -> web3.Web3:
    try:
        return web3_connection_manager.get_web3(chain_id=web3_connection_manager.default_chain_id)
    except DegenbotError:
        raise DegenbotError("A default Web3 instance has not been registered.") from None


def set_web3(w3: web3.Web3) -> None:
    if w3.is_connected() is False:
        raise DegenbotError("Web3 instance is not connected.")
    web3_connection_manager.register_web3(w3)
    web3_connection_manager.set_default_chain(w3.eth.chain_id)


web3_connection_manager = Web3ConnectionManager()
