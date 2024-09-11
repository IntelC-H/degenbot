from collections.abc import Sequence
from fractions import Fraction
from typing import Literal

import eth_abi.packed
from eth_typing import ChecksumAddress
from eth_utils.crypto import keccak
from hexbytes import HexBytes

from ..functions import eip_1167_clone_address


def _d(
    x0: int,
    y: int,
) -> int:
    return (3 * x0 * y * y // 10**36) + (x0 * x0 * x0 // 10**36)


def _f(
    x0: int,
    y: int,
) -> int:
    _a = x0 * y // 10**18
    _b = x0 * x0 // 10**18 + y * y // 10**18
    return (_a * _b) // 10**18


def _get_y_camelot(
    x_0: int,
    xy: int,
    y: int,
) -> int:
    for _ in range(255):
        y_prev = y
        k = _f(x_0, y)
        if k < xy:
            dy = (xy - k) * 10**18 // _d(x_0, y)
            y = y + dy
        else:
            dy = (k - xy) * 10**18 // _d(x_0, y)
            y = y - dy

        if y > y_prev:
            if y - y_prev <= 1:
                return y
        elif y_prev - y <= 1:
            return y

    return y


def _get_y_aerodrome(
    x0: int,
    xy: int,
    y: int,
):
    for _ in range(255):
        k = _f(x0, y)
        if k < xy:
            # there are two cases where dy == 0
            # case 1: The y is converged and we find the correct answer
            # case 2: _d(x0, y) is too large compare to (xy - k) and the rounding error
            #         screwed us.
            #         In this case, we need to increase y by 1
            dy = ((xy - k) * 1e18) // _d(x0, y)
            if dy == 0:
                if k == xy:
                    # We found the correct answer. Return y
                    return y
                if _k(x0, y + 1) > xy:
                    # If _k(x0, y + 1) > xy, then we are close to the correct answer.
                    # There's no closer answer than y + 1
                    return y + 1
                dy = 1
            y = y + dy
        else:
            dy = ((k - xy) * 1e18) // _d(x0, y)
            if dy == 0:
                if k == xy or _f(x0, y - 1) < xy:
                    # Likewise, if k == xy, we found the correct answer.
                    # If _f(x0, y - 1) < xy, then we are close to the correct answer.
                    # There's no closer answer than "y"
                    # It's worth mentioning that we need to find y where f(x0, y) >= xy
                    # As a result, we can't return y - 1 even it's closer to the correct answer
                    return y
                dy = 1
            y = y - dy


def _k(
    balance_0: int,
    balance_1: int,
    decimals_0: int,
    decimals_1: int,
) -> int:
    _x = balance_0 * 10**18 // decimals_0
    _y = balance_1 * 10**18 // decimals_1
    _a = _x * _y // 10**18
    _b = (_x * _x // 10**18) + (_y * _y // 10**18)
    return _a * _b // 10**18  # x^3*y + y^3*x >= k


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


def solidly_calc_exact_in_stable(
    amount_in: int,
    token_in: Literal[0, 1],
    reserves0: int,
    reserves1: int,
    decimals0: int,
    decimals1: int,
    fee: Fraction,
) -> int:
    """
    Calculate the amount out for an exact input from a Solidly stable pool with invariant
    y*x^3*y + x*y^3 = k.
    """

    _amount_in = amount_in * (fee.denominator - fee.numerator) // fee.denominator

    xy = _k(reserves0, reserves1)
    _reserve0 = (reserves0 * 10**18) // decimals0
    _reserve1 = (reserves1 * 10**18) // decimals1

    if token_in == 0:
        reserveA, reserveB = _reserve0, _reserve1
        amountIn = _amount_in * 10**18 // decimals0
    else:
        reserveA, reserveB = _reserve1, _reserve0
        amountIn = _amount_in * 10**18 // decimals1

    y = reserveB - _get_y_aerodrome(amountIn + reserveA, xy, reserveB)
    return y * (decimals1 if token_in == 0 else decimals0) // 10**18


def solidly_calc_exact_in_volatile(
    amount_in: int,
    token_in: Literal[0, 1],
    reserves0: int,
    reserves1: int,
    fee: Fraction,
) -> int:
    """
    Calculate the amount out for an exact input from a Solidly volatile pool with invariant
    (x*y=k).
    """

    amount_in_after_fee = amount_in - amount_in * fee

    if token_in == 0:
        reserveA, reserveB = reserves0, reserves1
    elif token_in == 1:
        reserveA, reserveB = reserves1, reserves0

    return (amount_in_after_fee * reserveB) // (reserveA + amount_in_after_fee)
