from brownie import Contract
from brownie.convert.datatypes import Wei
from scipy import optimize
from fractions import Fraction
from typing import List, Tuple
from ..liquiditypool import LiquidityPool
from ..token import Erc20Token

# TODO: improve arbitrage calculation for repaying with same token, instead of borrow A -> repay B


class FlashBorrowToLpSwapWithFuture:
    def __init__(
        self,
        borrow_pool: LiquidityPool,
        borrow_token: Erc20Token,
        repay_token: Erc20Token,
        swap_pool_addresses: List[str] = None,
        swap_pools: List[LiquidityPool] = None,
        name: str = "",
        update_method="polling",
    ):

        assert (
            swap_pools or swap_pool_addresses
        ), "At least one pool address or LiquidityPool object must be provided"
        assert not (
            swap_pool_addresses and swap_pools
        ), "Choose pool addresses or LiquidityPool objects, not both"

        assert update_method in [
            "polling",
            "external",
        ], "update_method must be 'polling' or 'external'"

        assert not (
            update_method == "external" and swap_pool_addresses
        ), "swap pools by address must be updated with the 'polling' method"

        self.borrow_pool = borrow_pool
        self.borrow_token = borrow_token
        self.repay_token = repay_token
        self._update_method = update_method

        # if the object was initialized with pool objects directly, use these directly
        if swap_pools:
            self.swap_pools = swap_pools

        # otherwise, create internal objects
        else:
            self.swap_pools = []
            for address in swap_pool_addresses:
                self.swap_pools.append(
                    LiquidityPool(
                        address=address,
                        update_method=update_method,
                    )
                )

        self.swap_pool_addresses = [pool.address for pool in self.swap_pools]
        self.swap_pool_tokens = [[pool.token0, pool.token1] for pool in self.swap_pools]

        self.all_pool_addresses = [
            pool.address for pool in self.swap_pools + [self.borrow_pool]
        ]

        if name:
            self.name = name
        else:
            self.name = (
                self.borrow_pool.name
                + " -> "
                + " -> ".join([pool.name for pool in self.swap_pools])
            )

        assert self.borrow_token.address in [
            self.swap_pools[0].token0.address,
            self.swap_pools[0].token1.address,
        ], "Borrowed token not found in the first swap pool"

        assert self.repay_token.address in [
            self.swap_pools[-1].token0.address,
            self.swap_pools[-1].token1.address,
        ], "Repay token not found in the last swap pool"

        if self.swap_pools[0].token0.address == borrow_token.address:
            forward_token_address = self.swap_pools[0].token1.address
        else:
            forward_token_address = self.swap_pools[0].token0.address

        token_in_address = forward_token_address
        for pool in self.swap_pools:
            if pool.token0.address == token_in_address:
                forward_token_address = pool.token1.address
            elif pool.token1.address == token_in_address:
                forward_token_address = pool.token0.address
            else:
                raise Exception("Swap pools are invalid, no swap route possible!")

        self.best = {
            "init": True,
            "borrow_amount": 0,
            "borrow_token": self.borrow_token,
            "borrow_pool": self.borrow_pool,
            "borrow_pool_amounts": [],
            "repay_amount": 0,
            "profit_amount": 0,
            "profit_token": self.repay_token,
            "swap_pools": self.swap_pools,
            "swap_pool_addresses": self.swap_pool_addresses,
            "swap_pool_amounts": [],
            "swap_pool_tokens": self.swap_pool_tokens,
        }

        self.best_future = {
            "borrow_amount": 0,
            "borrow_token": self.borrow_token,
            "borrow_pool": self.borrow_pool,
            "borrow_pool_amounts": [],
            "repay_amount": 0,
            "profit_amount": 0,
            "profit_token": self.repay_token,
            "swap_pools": self.swap_pools,
            "swap_pool_addresses": self.swap_pool_addresses,
            "swap_pool_amounts": [],
            "swap_pool_tokens": self.swap_pool_tokens,
        }

        self.update_reserves()

    def _build_multipool_amounts_out(
        self,
        token_in: Erc20Token,
        token_in_quantity: int,
        pool_overrides: list = [],
    ) -> List[List[int]]:

        number_of_pools = len(self.swap_pools)

        pools_amounts_out = []

        for i in range(number_of_pools):

            # determine the output token for pool0
            if token_in.address == self.swap_pools[i].token0.address:
                token_out = self.swap_pools[i].token1
            elif token_in.address == self.swap_pools[i].token1.address:
                token_out = self.swap_pools[i].token0
            else:
                print("wtf?")
                raise Exception

            # value of 0 will result in the default behavior (no reserve overrides)
            override_reserves_token0 = 0
            override_reserves_token1 = 0

            # override the reserves if found in pool_overrides
            for override in pool_overrides:
                if override[0] == self.swap_pools[i]:
                    override_reserves_token0, override_reserves_token1 = override[1]

            # calculate the swap output through pool[i]
            token_out_quantity = self.swap_pools[i].calculate_tokens_out_from_tokens_in(
                token_in=token_in,
                token_in_quantity=token_in_quantity,
                override_reserves_token0=override_reserves_token0,
                override_reserves_token1=override_reserves_token1,
            )

            if token_in.address == self.swap_pools[i].token0.address:
                pools_amounts_out.append([0, token_out_quantity])
            elif token_in.address == self.swap_pools[i].token1.address:
                pools_amounts_out.append([token_out_quantity, 0])

            if i == number_of_pools - 1:
                # if we've reached the last pool, return the pool_amounts_out list
                return pools_amounts_out
            else:
                # otherwise, feed the results back into the loop
                token_in = token_out
                token_in_quantity = token_out_quantity

    def _calculate_arbitrage(
        self,
        override_future: bool = False,
        pool_overrides: list = None,
    ):

        borrow_pool_reserves_token0 = self.borrow_pool.reserves_token0
        borrow_pool_reserves_token1 = self.borrow_pool.reserves_token1

        # override the borrowing pool reserves if any of the pools in pool_overrides
        # reference this pool
        if override_future:
            for override in pool_overrides:
                if override[0] == self.borrow_pool:
                    borrow_pool_reserves_token0 = override[1][0]
                    borrow_pool_reserves_token1 = override[1][1]
                    break

        # set up the boundaries for the Brent optimizer based on which token
        # is being borrowed
        if self.borrow_token.address == self.borrow_pool.token0.address:
            bounds = (
                0,
                borrow_pool_reserves_token0,
            )
            bracket = (
                0.001 * borrow_pool_reserves_token0,
                0.01 * borrow_pool_reserves_token0,
            )
        elif self.borrow_token.address == self.borrow_pool.token1.address:
            bounds = (
                0,
                borrow_pool_reserves_token1,
            )
            bracket = (
                0.001 * borrow_pool_reserves_token1,
                0.01 * borrow_pool_reserves_token1,
            )
        else:
            print("WTF? Could not identify borrow token")
            raise Exception

        opt = optimize.minimize_scalar(
            lambda x: -float(
                self.calculate_multipool_tokens_out_from_tokens_in(
                    token_in=self.borrow_token,
                    token_in_quantity=x,
                    pool_overrides=pool_overrides,
                )
                - self.borrow_pool.calculate_tokens_in_from_tokens_out(
                    token_in=self.repay_token,
                    token_out_quantity=x,
                    override_reserves_token0=borrow_pool_reserves_token0,
                    override_reserves_token1=borrow_pool_reserves_token1,
                )
            ),
            method="bounded",
            bounds=bounds,
            bracket=bracket,
            options={"xatol": 1.0},
        )

        best_borrow = int(opt.x)

        if self.borrow_token.address == self.borrow_pool.token0.address:
            borrow_amounts = [best_borrow, 0]
        elif self.borrow_token.address == self.borrow_pool.token1.address:
            borrow_amounts = [0, best_borrow]
        else:
            print("wtf?")
            raise Exception

        best_repay = self.borrow_pool.calculate_tokens_in_from_tokens_out(
            token_in=self.repay_token,
            token_out_quantity=best_borrow,
            override_reserves_token0=borrow_pool_reserves_token0,
            override_reserves_token1=borrow_pool_reserves_token1,
        )
        best_profit = -int(opt.fun)

        if override_future:
            if best_borrow > 0 and best_profit > 0:
                self.best_future.update(
                    {
                        "borrow_amount": best_borrow,
                        "borrow_pool_amounts": borrow_amounts,
                        "repay_amount": best_repay,
                        "profit_amount": best_profit,
                        "swap_pool_amounts": self._build_multipool_amounts_out(
                            token_in=self.borrow_token,
                            token_in_quantity=best_borrow,
                            pool_overrides=pool_overrides,
                        ),
                    }
                )
            else:
                self.best_future.update(
                    {
                        "borrow_amount": 0,
                        "borrow_pool_amounts": [],
                        "repay_amount": 0,
                        "profit_amount": 0,
                        "swap_pool_amounts": [],
                    }
                )
        else:
            # only save opportunities with rational, positive values
            if best_borrow > 0 and best_profit > 0:
                self.best.update(
                    {
                        "borrow_amount": best_borrow,
                        "borrow_pool_amounts": borrow_amounts,
                        "repay_amount": best_repay,
                        "profit_amount": best_profit,
                        "swap_pool_amounts": self._build_multipool_amounts_out(
                            token_in=self.borrow_token,
                            token_in_quantity=best_borrow,
                        ),
                    }
                )
            else:
                self.best.update(
                    {
                        "borrow_amount": 0,
                        "borrow_pool_amounts": [],
                        "repay_amount": 0,
                        "profit_amount": 0,
                        "swap_pool_amounts": [],
                    }
                )

    def __str__(self) -> str:
        return self.name

    def calculate_multipool_tokens_out_from_tokens_in(
        self,
        token_in: Erc20Token,
        token_in_quantity: int,
        pool_overrides: list = None,
    ) -> int:
        """
        Calculates the expected token OUTPUT from the last pool for a given token INPUT to the first pool
        at current pool reserves. Uses the self.token0 and self.token1 pointers to determine which token
        is being swapped in and uses the appropriate formula

        2022-06-14 update: add support for overriding pool reserves
        """

        number_of_pools = len(self.swap_pools)

        for i in range(number_of_pools):

            # determine the output token for pool0
            if token_in.address == self.swap_pools[i].token0.address:
                token_out = self.swap_pools[i].token1
            elif token_in.address == self.swap_pools[i].token1.address:
                token_out = self.swap_pools[i].token0
            else:
                print("wtf?")
                raise Exception

            override_reserves_token0 = 0
            override_reserves_token1 = 0

            for override_pool, override_reserves in pool_overrides:
                # each override is a tuple of form (LiquidityPool, (reserve0,reserve1))
                if override_pool == self.swap_pools[i]:
                    override_reserves_token0 = override_reserves[0]
                    override_reserves_token1 = override_reserves[1]
                    break

            # calculate the swap output through pool[i]
            token_out_quantity = self.swap_pools[i].calculate_tokens_out_from_tokens_in(
                token_in=token_in,
                token_in_quantity=token_in_quantity,
                override_reserves_token0=override_reserves_token0,
                override_reserves_token1=override_reserves_token1,
            )

            if i == number_of_pools - 1:
                # if we've reached the last pool, return the output amount
                return token_out_quantity
            else:
                # otherwise, use the output as input on the next loop
                token_in = token_out
                token_in_quantity = token_out_quantity

    def update_reserves(
        self,
        silent: bool = False,
        print_reserves: bool = True,
        print_ratios: bool = True,
        override_future: bool = False,
        pool_overrides: List[List[Tuple[LiquidityPool, Tuple[int]]]] = [],
    ) -> bool:
        """
        Checks each liquidity pool for updates by passing a call to .update_reserves(), which returns False if there are no updates.
        Will calculate arbitrage amounts only after checking all pools and finding a reason to update, or on startup (via the 'init' dictionary key)
        """
        recalculate = False

        if self._update_method != "external":

            # calculate initial arbitrage after the object is instantiated, otherwise proceed with normal checks
            if self.best["init"] == True:
                self.best["init"] = False
                recalculate = True

            # flag for recalculation if the borrowing pool has been updated
            if self.borrow_pool.update_reserves(
                silent=silent,
                print_reserves=print_reserves,
                print_ratios=print_ratios,
            ):
                recalculate = True

            # flag for recalculation if any of the pools along the swap path have been updated
            for pool in self.swap_pools:
                if pool.update_reserves(
                    silent=silent,
                    print_reserves=print_reserves,
                    print_ratios=print_ratios,
                ):
                    recalculate = True

        if override_future:
            recalculate = True
            assert pool_overrides, "Overrides must be provided!"
            assert len(pool_overrides) <= len(self.swap_pools) + 1
            for override_pool, override_reserves in pool_overrides:
                assert (
                    type(override_pool) is LiquidityPool
                ), "override does not include a LiquidityPool object!"
                assert (
                    type(override_reserves) is tuple
                ), "overrides not formatted as a tuple"
                assert len(override_reserves) == 2, "override length must be 2"
                assert type(override_reserves[0]) in (
                    int,
                    Wei,
                ), f"override for token0 must be int/Wei, is {type(override_reserves[0])}"
                assert type(override_reserves[1]) in (
                    int,
                    Wei,
                ), f"override for token1 must be int/Wei, is {type(override_reserves[1])}"
        else:
            assert (
                not pool_overrides
            ), "Must not provide overrides without override_future = True"

        if self.borrow_pool.new_reserves:
            recalculate = True
        for pool in self.swap_pools:
            if pool.new_reserves:
                recalculate = True
                break

        if recalculate:
            self._calculate_arbitrage(
                override_future=override_future,
                pool_overrides=pool_overrides,
            )
            return True
        else:
            return False
