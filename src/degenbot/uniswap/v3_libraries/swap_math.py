from . import full_math as FullMath
from . import sqrt_price_math as SqrtPriceMath


def computeSwapStep(
    sqrt_ratio_x96_current: int,
    sqrt_ratio_x96_target: int,
    liquidity: int,
    amount_remaining: int,
    fee_pips: int,
) -> tuple[int, int, int, int]:
    zero_for_one: bool = sqrt_ratio_x96_current >= sqrt_ratio_x96_target
    exact_in: bool = amount_remaining >= 0
    amount_in = 0
    amount_out = 0

    if exact_in:
        amount_remaining_minus_fee: int = FullMath.muldiv(amount_remaining, 10**6 - fee_pips, 10**6)
        amount_in = (
            SqrtPriceMath.get_amount0_delta(
                sqrt_ratio_x96_target, sqrt_ratio_x96_current, liquidity, True
            )
            if zero_for_one
            else SqrtPriceMath.get_amount1_delta(
                sqrt_ratio_x96_current, sqrt_ratio_x96_target, liquidity, True
            )
        )
        if amount_remaining_minus_fee >= amount_in:
            sqrt_ratio_x96_next = sqrt_ratio_x96_target
        else:
            sqrt_ratio_x96_next = SqrtPriceMath.get_next_sqrt_price_from_input(
                sqrt_ratio_x96_current,
                liquidity,
                amount_remaining_minus_fee,
                zero_for_one,
            )
    else:
        amount_out = (
            SqrtPriceMath.get_amount1_delta(
                sqrt_ratio_x96_target, sqrt_ratio_x96_current, liquidity, False
            )
            if zero_for_one
            else SqrtPriceMath.get_amount0_delta(
                sqrt_ratio_x96_current, sqrt_ratio_x96_target, liquidity, False
            )
        )
        if -amount_remaining >= amount_out:
            sqrt_ratio_x96_next = sqrt_ratio_x96_target
        else:
            sqrt_ratio_x96_next = SqrtPriceMath.get_next_sqrt_price_from_output(
                sqrt_ratio_x96_current,
                liquidity,
                -amount_remaining,
                zero_for_one,
            )

    reached_target_price = sqrt_ratio_x96_target == sqrt_ratio_x96_next
    # get the input/output amounts
    if zero_for_one:
        amount_in = (
            amount_in
            if (reached_target_price and exact_in)
            else SqrtPriceMath.get_amount0_delta(
                sqrt_ratio_x96_next, sqrt_ratio_x96_current, liquidity, True
            )
        )
        amount_out = (
            amount_out
            if (reached_target_price and not exact_in)
            else SqrtPriceMath.get_amount1_delta(
                sqrt_ratio_x96_next, sqrt_ratio_x96_current, liquidity, False
            )
        )
    else:
        amount_in = (
            amount_in
            if (reached_target_price and exact_in)
            else SqrtPriceMath.get_amount1_delta(
                sqrt_ratio_x96_current, sqrt_ratio_x96_next, liquidity, True
            )
        )
        amount_out = (
            amount_out
            if (reached_target_price and not exact_in)
            else SqrtPriceMath.get_amount0_delta(
                sqrt_ratio_x96_current, sqrt_ratio_x96_next, liquidity, False
            )
        )

    # cap the output amount to not exceed the remaining output amount
    if not exact_in and (amount_out > -amount_remaining):
        amount_out = -amount_remaining

    if exact_in and (sqrt_ratio_x96_next != sqrt_ratio_x96_target):
        # we didn't reach the target, so take the remainder of the maximum input as fee
        fee_amount = amount_remaining - amount_in
    else:
        fee_amount = FullMath.muldiv_rounding_up(amount_in, fee_pips, 10**6 - fee_pips)

    return (
        sqrt_ratio_x96_next,
        amount_in,
        amount_out,
        fee_amount,
    )
