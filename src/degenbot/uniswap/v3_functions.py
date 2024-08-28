from collections.abc import Callable, Iterable, Iterator
from fractions import Fraction
from itertools import cycle

import eth_abi.abi
from eth_typing import ChecksumAddress
from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak
from hexbytes import HexBytes

from ..functions import create2_address


def decode_v3_path(path: bytes) -> list[ChecksumAddress | int]:
    """
    Decode the `path` bytes used by the Uniswap V3 Router/Router2 contracts. `path` is a
    close-packed encoding of 20 byte pool addresses, interleaved with 3 byte fees.
    """
    ADDRESS_BYTES = 20
    FEE_BYTES = 3

    def _extract_address(chunk: bytes) -> ChecksumAddress:
        return to_checksum_address(chunk)

    def _extract_fee(chunk: bytes) -> int:
        return int.from_bytes(chunk, byteorder="big")

    if any(
        [
            len(path) < ADDRESS_BYTES + FEE_BYTES + ADDRESS_BYTES,
            len(path) % (ADDRESS_BYTES + FEE_BYTES) != ADDRESS_BYTES,
        ]
    ):  # pragma: no cover
        raise ValueError("Invalid path.")

    chunk_length_and_decoder_function: Iterator[
        tuple[
            int,
            Callable[
                [bytes],
                ChecksumAddress | int,
            ],
        ]
    ] = cycle(
        [
            (ADDRESS_BYTES, _extract_address),
            (FEE_BYTES, _extract_fee),
        ]
    )

    path_offset = 0
    decoded_path: list[ChecksumAddress | int] = []
    while path_offset != len(path):
        byte_length, extraction_func = next(chunk_length_and_decoder_function)
        chunk = HexBytes(path[path_offset : path_offset + byte_length])
        decoded_path.append(extraction_func(chunk))
        path_offset += byte_length

    return decoded_path


def exchange_rate_from_sqrt_price_x96(sqrt_price_x96: int) -> Fraction:
    # ref: https://blog.uniswap.org/uniswap-v3-math-primer
    return Fraction(sqrt_price_x96**2, 2**192)


def generate_v3_pool_address(
    deployer_address: str | bytes,
    token_addresses: Iterable[str | bytes],
    fee: int,
    init_hash: str | bytes,
) -> ChecksumAddress:
    """
    Get the deterministic V3 pool address generated by CREATE2. Uses the token address and fee to
    generate the salt. The token addresses can be passed in any order.

    Adapted from https://github.com/Uniswap/v3-periphery/blob/0682387198a24c7cd63566a2c58398533860a5d1/contracts/libraries/PoolAddress.sol#L33
    """

    token_addresses = sorted([HexBytes(address) for address in token_addresses])

    salt = keccak(
        eth_abi.abi.encode(
            ("address", "address", "uint24"),
            (*token_addresses, fee),
        )
    )

    return create2_address(
        deployer=deployer_address,
        salt=salt,
        bytecode=init_hash,
    )
