from typing import Optional, Union

from eth_typing import ChecksumAddress
from eth_utils.address import to_checksum_address
from web3.contract import Contract

from .. import config
from .abi import UNISWAP_V3_TICKLENS_ABI


class TickLens:
    def __init__(
        self,
        address: Union[str, ChecksumAddress],
        abi: Optional[list] = None,
    ):
        self.address = to_checksum_address(address)
        self.abi = abi if abi is not None else UNISWAP_V3_TICKLENS_ABI

    @property
    def _w3_contract(self) -> Contract:
        return config.get_web3().eth.contract(
            address=self.address,
            abi=self.abi,
        )
