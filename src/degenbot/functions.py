import eth_account.messages
import web3
from eth_account.datastructures import SignedMessage
from eth_typing import ChecksumAddress
from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak
from hexbytes import HexBytes
from web3.types import BlockIdentifier

from . import config


def create2_address(
    deployer: str | bytes,
    salt: bytes | str,
    bytecode: bytes | str,
) -> ChecksumAddress:
    """
    Generate the deterministic CREATE2 address for a given deployer, salt, and contract creation
    bytecode.

    Reference: https://docs.openzeppelin.com/cli/2.8/deploying-with-create2
    """

    CREATE2_PREFIX = 0xFF

    return to_checksum_address(
        keccak(
            HexBytes(CREATE2_PREFIX) + HexBytes(deployer) + HexBytes(salt) + HexBytes(bytecode),
        )[-20:],  # Contract address is the last 20 bytes from the 32 byte hash
    )


def eip_191_hash(message: str, private_key: str) -> str:
    """
    Get the signature hash (a hex-formatted string) for a given message and signing key.
    """
    result: SignedMessage = eth_account.Account.sign_message(
        signable_message=eth_account.messages.encode_defunct(
            text=web3.Web3.keccak(text=message).hex()
        ),
        private_key=private_key,
    )
    return result.signature.hex()


def get_number_for_block_identifier(identifier: BlockIdentifier | None) -> int:
    match identifier:
        case None:
            return config.get_web3().eth.get_block_number()
        case int():
            return identifier
        case bytes():
            return int.from_bytes(identifier, byteorder="big")
        case str() if isinstance(identifier, str) and identifier[:2] == "0x" and len(
            identifier
        ) == 66:
            return int(identifier, 16)
        case "latest" | "earliest" | "pending" | "safe" | "finalized":
            # These tags vary with each new block, so translate to a fixed block number
            return config.get_web3().eth.get_block(identifier)["number"]
        case _:
            raise ValueError(f"Invalid block identifier {identifier!r}")


def next_base_fee(
    parent_base_fee: int,
    parent_gas_used: int,
    parent_gas_limit: int,
    min_base_fee: int | None = None,
    base_fee_max_change_denominator: int = 8,
    elasticity_multiplier: int = 2,
) -> int:
    """
    Calculate next base fee for an EIP-1559 compatible blockchain. The
    formula is taken from the example code in the EIP-1559 proposal (ref:
    https://eips.ethereum.org/EIPS/eip-1559).

    The default values for `base_fee_max_change_denominator` and
    `elasticity_multiplier` are taken from EIP-1559.

    Enforces `min_base_fee` if provided.
    """

    last_gas_target = parent_gas_limit // elasticity_multiplier

    if parent_gas_used == last_gas_target:
        next_base_fee = parent_base_fee
    elif parent_gas_used > last_gas_target:
        gas_used_delta = parent_gas_used - last_gas_target
        base_fee_delta = max(
            parent_base_fee * gas_used_delta // last_gas_target // base_fee_max_change_denominator,
            1,
        )
        next_base_fee = parent_base_fee + base_fee_delta
    else:
        gas_used_delta = last_gas_target - parent_gas_used
        base_fee_delta = (
            parent_base_fee * gas_used_delta // last_gas_target // base_fee_max_change_denominator
        )
        next_base_fee = parent_base_fee - base_fee_delta

    return max(min_base_fee, next_base_fee) if min_base_fee else next_base_fee
