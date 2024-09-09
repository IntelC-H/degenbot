from collections.abc import Sequence

import eth_abi.packed
from eth_typing import ChecksumAddress
from eth_utils.crypto import keccak
from hexbytes import HexBytes

from ..functions import eip_1167_clone_address


def generate_aerodrome_v2_pool_address(
    deployer_address: str | bytes,
    token_addresses: Sequence[str | bytes],
    implementation_address: str | bytes,
    stable: bool,
) -> ChecksumAddress:
    """
    Get the deterministic V2 pool address generated by CREATE2. Uses the token address to generate
    the salt. The token addresses can be passed in any order.

    Adapted from https://github.com/aerodrome-finance/contracts/blob/main/contracts/factories/PoolFactory.sol
    and https://github.com/OpenZeppelin/openzeppelin-contracts/blob/master/contracts/proxy/Clones.sol
    """

    sorted_token_addresses = sorted([HexBytes(address) for address in token_addresses])

    salt = keccak(
        eth_abi.packed.encode_packed(
            ("address", "address", "bool"),
            [*sorted_token_addresses, stable],
        )
    )

    return eip_1167_clone_address(
        deployer=deployer_address,
        implementation_contract=implementation_address,
        salt=salt,
    )