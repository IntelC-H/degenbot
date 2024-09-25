# TODO: add event prototype exporter method and handler for callbacks

import dataclasses
from bisect import bisect_left
from fractions import Fraction
from threading import Lock
from typing import TYPE_CHECKING, Any, cast

from eth_typing import ChecksumAddress
from eth_utils.address import to_checksum_address
from typing_extensions import override
from web3 import Web3
from web3.types import BlockIdentifier

from .. import config
from ..erc20_token import Erc20Token
from ..exceptions import (
    BitmapWordUnavailableError,
    EVMRevertError,
    ExternalUpdateError,
    InsufficientAmountOutError,
    LiquidityPoolError,
    NoPoolStateAvailable,
)
from ..exchanges.uniswap.deployments import FACTORY_DEPLOYMENTS
from ..exchanges.uniswap.types import UniswapV3ExchangeDeployment
from ..functions import encode_function_calldata, get_number_for_block_identifier, raw_call
from ..logging import logger
from ..manager.token_manager import Erc20TokenHelperManager
from ..registry.all_pools import AllPools
from ..types import AbstractLiquidityPool
from .v3_functions import (
    exchange_rate_from_sqrt_price_x96,
    generate_aerodrome_v3_pool_address,
    generate_v3_pool_address,
    get_tick_word_and_bit_position,
)
from .v3_libraries import LiquidityMath, SwapMath, TickBitmap, TickMath
from .v3_libraries.functions import to_int256
from .v3_types import (
    UniswapV3BitmapAtWord,
    UniswapV3LiquidityAtTick,
    UniswapV3PoolExternalUpdate,
    UniswapV3PoolSimulationResult,
    UniswapV3PoolState,
    UniswapV3PoolStateUpdated,
)

UNISWAP_V3_MAINNET_POOL_INIT_HASH = (
    "0xe34f199b19b2b4f47f68442619d555527d244f78a3297ea89325f843f87b8b54"
)


class V3LiquidityPool(AbstractLiquidityPool):
    @dataclasses.dataclass(slots=True, repr=False, eq=False)
    class SwapCache:
        liquidity_start: int
        tick_cumulative: int

    @dataclasses.dataclass(slots=True, repr=False, eq=False)
    class SwapState:
        amount_specified_remaining: int
        amount_calculated: int
        sqrt_price_x96: int
        tick: int
        liquidity: int

    @dataclasses.dataclass(slots=True, repr=False, eq=False)
    class StepComputations:
        sqrt_price_start_x96: int = 0
        tick_next: int = 0
        initialized: bool = False
        sqrt_price_next_x96: int = 0
        amount_in: int = 0
        amount_out: int = 0
        fee_amount: int = 0

    def __init__(
        self,
        address: str,
        fee: int | None = None,
        tick_spacing: int | None = None,
        tokens: list[Erc20Token] | None = None,
        factory_address: str | None = None,
        deployer_address: str | None = None,
        init_hash: str | None = None,
        silent: bool = False,
        tick_data: dict[int, dict[str, Any] | UniswapV3LiquidityAtTick] | None = None,
        tick_bitmap: dict[int, dict[str, Any] | UniswapV3BitmapAtWord] | None = None,
        state_block: int | None = None,
        archive_states: bool = True,
        verify_address: bool = True,
    ):
        self.address = to_checksum_address(address)

        self._state_lock = Lock()
        self._state = UniswapV3PoolState(
            pool=self.address,
            liquidity=0,
            sqrt_price_x96=0,
            tick=0,
            tick_bitmap={},
            tick_data={},
        )

        w3 = config.get_web3()
        self._update_block = state_block if state_block is not None else w3.eth.block_number

        if factory_address is not None:
            self.factory = to_checksum_address(factory_address)
        else:
            factory_address, *_ = raw_call(
                w3=w3,
                block_identifier=self._update_block,
                address=self.address,
                calldata=encode_function_calldata(
                    function_prototype="factory()",
                    function_arguments=None,
                ),
                return_types=["address"],
            )
            self.factory = to_checksum_address(factory_address)

        if deployer_address is not None:
            self.deployer_address = to_checksum_address(deployer_address)
        else:
            self.deployer_address = self.factory

        chain_id = w3.eth.chain_id
        try:
            # Use degenbot deployment values if available
            factory_deployment = FACTORY_DEPLOYMENTS[chain_id][self.factory]
            self.init_hash = factory_deployment.pool_init_hash
            if factory_deployment.deployer is not None:
                self.deployer_address = factory_deployment.deployer
        except KeyError:
            # Deployment is unknown. Uses any inputs provided, otherwise use default values from
            # original Uniswap contracts
            self.init_hash = (
                init_hash if init_hash is not None else UNISWAP_V3_MAINNET_POOL_INIT_HASH
            )

        if tokens is not None:
            self.token0, self.token1 = sorted(tokens)
        else:
            token0_address, *_ = raw_call(
                w3=w3,
                address=self.address,
                calldata=encode_function_calldata(
                    function_prototype="token0()",
                    function_arguments=None,
                ),
                return_types=["address"],
                block_identifier=self._update_block,
            )
            token1_address, *_ = raw_call(
                w3=w3,
                address=self.address,
                calldata=encode_function_calldata(
                    function_prototype="token1()",
                    function_arguments=None,
                ),
                return_types=["address"],
                block_identifier=self._update_block,
            )

            token_manager = Erc20TokenHelperManager(chain_id)
            self.token0 = token_manager.get_erc20token(
                address=token0_address,
                silent=silent,
            )
            self.token1 = token_manager.get_erc20token(
                address=token1_address,
                silent=silent,
            )

        self.tokens = (self.token0, self.token1)

        self.fee = fee if fee is not None else self.get_fee(w3=w3)
        self.tick_spacing = (
            tick_spacing if tick_spacing is not None else self.get_tick_spacing(w3=w3)
        )

        if verify_address and self.address != self._verified_address():  # pragma: no branch
            raise ValueError("Pool address verification failed.")

        self.name = f"{self.token0}-{self.token1} (V3, {self.fee / 10000:.2f}%)"

        if (tick_bitmap is not None) != (tick_data is not None):
            raise ValueError("Provide both tick_bitmap and tick_data.")

        # If liquidity info was not provided, treat the mapping as sparse
        self.sparse_liquidity_map = tick_bitmap is None or tick_data is None

        if tick_bitmap is not None:
            # transform dict to UniswapV3BitmapAtWord
            self.tick_bitmap = {
                int(word): (
                    UniswapV3BitmapAtWord(**bitmap_at_word)
                    if not isinstance(
                        bitmap_at_word,
                        UniswapV3BitmapAtWord,
                    )
                    else bitmap_at_word
                )
                for word, bitmap_at_word in tick_bitmap.items()
            }

            # Add empty regions to mapping
            min_word_position, _ = get_tick_word_and_bit_position(
                TickMath.MIN_TICK, self.tick_spacing
            )
            max_word_position, _ = get_tick_word_and_bit_position(
                TickMath.MAX_TICK, self.tick_spacing
            )
            known_empty_words = (
                set(range(min_word_position, max_word_position + 1)) - self.tick_bitmap.keys()
            )
            self.tick_bitmap.update({word: UniswapV3BitmapAtWord() for word in known_empty_words})

        if tick_data is not None:
            # transform dict to LiquidityAtTick
            self.tick_data = {
                int(tick): (
                    UniswapV3LiquidityAtTick(**liquidity_at_tick)
                    if not isinstance(
                        liquidity_at_tick,
                        UniswapV3LiquidityAtTick,
                    )
                    else liquidity_at_tick
                )
                for tick, liquidity_at_tick in tick_data.items()
            }

        if tick_bitmap is None and tick_data is None:
            logger.debug(f"{self} @ {self.address} updating -> {tick_bitmap=}, {tick_data=}")
            word_position, _ = get_tick_word_and_bit_position(self.tick, self.tick_spacing)

            self._fetch_tick_data_at_word(
                word_position,
                block_number=self.update_block,
            )

        self.liquidity = self.get_liquidity(w3=w3, block_identifier=self.update_block)
        self.sqrt_price_x96, self.tick, *_ = self.get_price_and_tick(
            w3=w3, block_identifier=self.update_block
        )

        self._pool_state_archive = {self.update_block: self.state} if archive_states else None

        AllPools(chain_id)[self.address] = self

        self._subscribers = set()

        if not silent:  # pragma: no branch
            logger.info(self.name)
            logger.info(f"• Token 0: {self.token0}")
            logger.info(f"• Token 1: {self.token1}")
            logger.info(f"• Fee: {self.fee}")
            logger.info(f"• Liquidity: {self.liquidity}")
            logger.info(f"• SqrtPrice: {self.sqrt_price_x96}")
            logger.info(f"• Tick: {self.tick}")

    @classmethod
    def from_exchange(
        cls,
        address: str,
        exchange: UniswapV3ExchangeDeployment,
        **kwargs: Any,
    ) -> "V3LiquidityPool":
        """
        Create a new `V3LiquidityPool` with exchange information taken from the provided deployment.
        """

        for key in [
            "factory_address",
            "deployer_address",
            "init_hash",
        ]:  # pragma: no cover
            if key in kwargs:
                logger.warning(
                    f"Ignoring keyword argument {key}={kwargs[key]} in favor of value in exchange deployment."  # noqa: E501
                )
                kwargs.pop(key)

        return cls(
            address=address,
            factory_address=exchange.factory.address,
            deployer_address=exchange.factory.deployer,
            init_hash=exchange.factory.pool_init_hash,
            **kwargs,
        )

    def __getstate__(self) -> dict[str, Any]:
        # Remove objects that cannot be pickled and are unnecessary to perform
        # the calculation
        copied_attributes = (
            "tick_bitmap",
            "tick_data",
        )

        dropped_attributes = (
            "_contract",
            "_state_lock",
            "_subscribers",
            "lens",
        )

        with self._state_lock:
            return {
                k: (v.copy() if k in copied_attributes else v)
                for k, v in self.__dict__.items()
                if k not in dropped_attributes
            }

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"V3LiquidityPool(address={self.address}, token0={self.token0}, token1={self.token1}, "
            f"fee={self.fee})"
        )

    def __str__(self) -> str:
        return self.name

    def _calculate_swap(
        self,
        zero_for_one: bool,
        amount_specified: int,
        sqrt_price_limit_x96: int,
        override_state: UniswapV3PoolState | None = None,
    ) -> tuple[int, int, int, int, int]:
        """
        This function is ported and adapted from the UniswapV3Pool.sol contract at
        https://github.com/Uniswap/v3-core/blob/main/contracts/UniswapV3Pool.sol

        Returns a tuple with amounts and final pool state values for a successful swap:
        (amount0, amount1, sqrt_price_x96, liquidity, tick)

        A negative amount indicates the token quantity sent to the swapper, and a positive amount
        indicates the token quantity deposited.
        """

        if amount_specified == 0:  # pragma: no branch
            raise EVMRevertError("AS")

        _liquidity = override_state.liquidity if override_state is not None else self.liquidity
        _sqrt_price_x96 = (
            override_state.sqrt_price_x96 if override_state is not None else self.sqrt_price_x96
        )
        _tick = override_state.tick if override_state is not None else self.tick
        _tick_bitmap = (
            override_state.tick_bitmap
            if override_state is not None and override_state.tick_bitmap is not None
            else self.tick_bitmap
        )
        _tick_data = (
            override_state.tick_data
            if override_state is not None and override_state.tick_data is not None
            else self.tick_data
        )

        if zero_for_one is True and not (
            TickMath.MIN_SQRT_RATIO < sqrt_price_limit_x96 < _sqrt_price_x96
        ):  # pragma: no cover
            raise EVMRevertError("SPL")

        if zero_for_one is False and not (
            _sqrt_price_x96 < sqrt_price_limit_x96 < TickMath.MAX_SQRT_RATIO
        ):  # pragma: no cover
            raise EVMRevertError("SPL")

        exact_input = amount_specified > 0
        cache = self.SwapCache(liquidity_start=_liquidity, tick_cumulative=0)
        state = self.SwapState(
            amount_specified_remaining=amount_specified,
            amount_calculated=0,
            sqrt_price_x96=_sqrt_price_x96,
            tick=_tick,
            liquidity=cache.liquidity_start,
        )

        while (
            state.amount_specified_remaining != 0 and state.sqrt_price_x96 != sqrt_price_limit_x96
        ):
            step = self.StepComputations()
            step.sqrt_price_start_x96 = state.sqrt_price_x96

            try:
                step.tick_next, step.initialized = TickBitmap.nextInitializedTickWithinOneWord(
                    _tick_bitmap,
                    state.tick,
                    self.tick_spacing,
                    zero_for_one,
                )
            except BitmapWordUnavailableError as exc:
                _, missing_word = exc.args
                assert (
                    self.sparse_liquidity_map is True
                )  # non-sparse liquidity mappings should populate all bitmaps in the constructor
                self._fetch_tick_data_at_word(
                    word_position=missing_word,
                    block_number=self.update_block,
                )
                continue

            # Ensure that we do not overshoot the min/max tick, as the tick bitmap is not aware of
            # these bounds
            if step.tick_next < TickMath.MIN_TICK:
                step.tick_next = TickMath.MIN_TICK
            elif step.tick_next > TickMath.MAX_TICK:
                step.tick_next = TickMath.MAX_TICK

            step.sqrt_price_next_x96 = TickMath.get_sqrt_ratio_at_tick(step.tick_next)

            # compute values to swap to the target tick, price limit, or point where input/output
            # amount is exhausted
            state.sqrt_price_x96, step.amount_in, step.amount_out, step.fee_amount = (
                SwapMath.computeSwapStep(
                    state.sqrt_price_x96,
                    sqrt_price_limit_x96
                    if (
                        (zero_for_one is True and step.sqrt_price_next_x96 < sqrt_price_limit_x96)
                        or (
                            zero_for_one is False
                            and step.sqrt_price_next_x96 > sqrt_price_limit_x96
                        )
                    )
                    else step.sqrt_price_next_x96,
                    state.liquidity,
                    state.amount_specified_remaining,
                    self.fee,
                )
            )

            if exact_input:
                state.amount_specified_remaining -= to_int256(step.amount_in + step.fee_amount)
                state.amount_calculated = to_int256(state.amount_calculated - step.amount_out)
            else:
                state.amount_specified_remaining += to_int256(step.amount_out)
                state.amount_calculated = to_int256(
                    state.amount_calculated + step.amount_in + step.fee_amount
                )

            # Shift tick if we reached the next price
            if state.sqrt_price_x96 == step.sqrt_price_next_x96:  # pragma: no branch
                # If the tick is initialized, run the tick transition
                if step.initialized:
                    liquidity_net_at_next_tick = _tick_data[step.tick_next].liquidityNet
                    if zero_for_one:
                        liquidity_net_at_next_tick = -liquidity_net_at_next_tick
                    state.liquidity = LiquidityMath.addDelta(
                        state.liquidity, liquidity_net_at_next_tick
                    )
                state.tick = step.tick_next - 1 if zero_for_one else step.tick_next

            elif state.sqrt_price_x96 != step.sqrt_price_start_x96:  # pragma: no branch
                # Recompute unless we're on a lower tick boundary (i.e. already transitioned ticks),
                # and haven't moved
                state.tick = TickMath.get_tick_at_sqrt_ratio(state.sqrt_price_x96)

        amount0, amount1 = (
            (
                amount_specified - state.amount_specified_remaining,
                state.amount_calculated,
            )
            if zero_for_one == exact_input
            else (
                state.amount_calculated,
                amount_specified - state.amount_specified_remaining,
            )
        )

        return amount0, amount1, state.sqrt_price_x96, state.liquidity, state.tick

    def get_tick_bitmap_at_word(
        self, w3: Web3, word_position: int, block_identifier: BlockIdentifier
    ) -> int:
        bitmap_at_word, *_ = raw_call(
            w3=w3,
            address=self.address,
            calldata=encode_function_calldata(
                function_prototype="tickBitmap(int16)",
                function_arguments=[word_position],
            ),
            return_types=["uint256"],
            block_identifier=block_identifier,
        )
        return cast(int, bitmap_at_word)

    def get_populated_ticks_in_word(
        self,
        w3: Web3,
        word_position: int,
        block_identifier: BlockIdentifier,
    ) -> list[tuple[int, int, int]]:
        bitmap_at_word = self.get_tick_bitmap_at_word(
            w3=w3,
            word_position=word_position,
            block_identifier=block_identifier,
        )

        populated_ticks = []
        for i in range(256):
            if bitmap_at_word & (1 << i) > 0:
                populatedTick = ((word_position << 8) + i) * self.tick_spacing
                liquidityGross, liquidityNet, *_ = raw_call(
                    w3=w3,
                    address=self.address,
                    calldata=encode_function_calldata(
                        function_prototype="ticks(int24)",
                        function_arguments=[populatedTick],
                    ),
                    # UNISWAP V3
                    return_types=[
                        "uint128",
                        "int128",
                        "uint256",
                        "uint256",
                        "int56",
                        "uint160",
                        "uint32",
                        "bool",
                    ],
                )
                populated_ticks.append(
                    (
                        populatedTick,
                        liquidityNet,
                        liquidityGross,
                    )
                )

        return populated_ticks

    def _fetch_tick_data_at_word(
        self,
        word_position: int,
        block_number: int | None = None,
    ) -> None:
        """
        Update the initialized tick values within a specified word position. A word is divided into
        256 ticks, spaced per the tickSpacing interval.
        """

        w3 = config.get_web3()

        if block_number is None:
            block_number = w3.eth.get_block_number()

        _tick_bitmap: int = 0
        _tick_data: list[tuple[int, int, int]] = []
        _tick_bitmap = self.get_tick_bitmap_at_word(
            w3=w3, word_position=word_position, block_identifier=block_number
        )
        if _tick_bitmap != 0:
            _tick_data = self.get_populated_ticks_in_word(
                w3=w3,
                word_position=word_position,
                block_identifier=block_number,
            )

        self.tick_bitmap[word_position] = UniswapV3BitmapAtWord(
            bitmap=_tick_bitmap,
            block=block_number,
        )
        for tick, liquidity_net, liquidity_gross in _tick_data:
            self.tick_data[tick] = UniswapV3LiquidityAtTick(
                liquidityNet=liquidity_net,
                liquidityGross=liquidity_gross,
                block=block_number,
            )

    def _verified_address(self) -> ChecksumAddress:
        return generate_v3_pool_address(
            deployer_address=self.deployer_address,
            token_addresses=(self.token0.address, self.token1.address),
            fee=self.fee,
            init_hash=self.init_hash,
        )

    @property
    def liquidity(self) -> int:
        return self.state.liquidity

    @liquidity.setter
    def liquidity(self, new_liquidity: int) -> None:
        current_state = self.state
        self.state = UniswapV3PoolState(
            pool=current_state.pool,
            liquidity=new_liquidity,
            sqrt_price_x96=current_state.sqrt_price_x96,
            tick=current_state.tick,
            tick_bitmap=current_state.tick_bitmap,
            tick_data=current_state.tick_data,
        )

    @property
    def sqrt_price_x96(self) -> int:
        return self.state.sqrt_price_x96

    @sqrt_price_x96.setter
    def sqrt_price_x96(self, new_sqrt_price_x96: int) -> None:
        current_state = self.state
        self.state = UniswapV3PoolState(
            pool=current_state.pool,
            liquidity=current_state.liquidity,
            sqrt_price_x96=new_sqrt_price_x96,
            tick=current_state.tick,
            tick_bitmap=current_state.tick_bitmap,
            tick_data=current_state.tick_data,
        )

    @property
    @override
    def state(self) -> UniswapV3PoolState:
        return self._state

    @state.setter
    @override
    def state(self, new_state: UniswapV3PoolState) -> None:
        self._state = new_state

    @property
    def tick(self) -> int:
        return self.state.tick

    @tick.setter
    def tick(self, new_tick: int) -> None:
        current_state = self.state
        self.state = UniswapV3PoolState(
            pool=current_state.pool,
            liquidity=current_state.liquidity,
            sqrt_price_x96=current_state.sqrt_price_x96,
            tick=new_tick,
            tick_bitmap=current_state.tick_bitmap,
            tick_data=current_state.tick_data,
        )

    @property
    def tick_bitmap(self) -> dict[int, UniswapV3BitmapAtWord]:
        if TYPE_CHECKING:
            # TODO: remove this check once tick_bitmap and tick_data are not optional
            assert self.state.tick_bitmap is not None
        return self.state.tick_bitmap

    @tick_bitmap.setter
    def tick_bitmap(self, new_tick_bitmap: dict[int, UniswapV3BitmapAtWord]) -> None:
        current_state = self.state
        self.state = UniswapV3PoolState(
            pool=current_state.pool,
            liquidity=current_state.liquidity,
            sqrt_price_x96=current_state.sqrt_price_x96,
            tick=current_state.tick,
            tick_bitmap=new_tick_bitmap,
            tick_data=current_state.tick_data,
        )

    @property
    def tick_data(self) -> dict[int, UniswapV3LiquidityAtTick]:
        if TYPE_CHECKING:
            # TODO: remove this check once tick_bitmap and tick_data are not optional
            assert self.state.tick_data is not None
        return self.state.tick_data

    @tick_data.setter
    def tick_data(self, new_tick_data: dict[int, UniswapV3LiquidityAtTick]) -> None:
        current_state = self.state
        self.state = UniswapV3PoolState(
            pool=current_state.pool,
            liquidity=current_state.liquidity,
            sqrt_price_x96=current_state.sqrt_price_x96,
            tick=current_state.tick,
            tick_bitmap=current_state.tick_bitmap,
            tick_data=new_tick_data,
        )

    @property
    def update_block(self) -> int:
        return self._update_block

    def auto_update(
        self,
        block_number: int | None = None,
        silent: bool = True,
    ) -> tuple[bool, UniswapV3PoolState]:
        """
        Retrieves the current slot0 and liquidity values from the LP, stores any that have changed,
        and returns a tuple with a status boolean indicating whether any update was found,
        and a dictionary holding current state values:
            - liquidity
            - sqrt_price_x96
            - tick

        Uses a lock to guard state-modifying methods that might cause race conditions
        when used with threads.
        """

        with self._state_lock:
            updated = False

            w3 = config.get_web3()
            block_number = w3.eth.get_block_number() if block_number is None else block_number

            _sqrt_price_x96, _tick, *_ = self.get_price_and_tick(
                w3=w3, block_identifier=block_number
            )
            _liquidity = self.get_liquidity(w3=w3, block_identifier=block_number)

            if self.sqrt_price_x96 != _sqrt_price_x96:
                updated = True
                self.sqrt_price_x96 = _sqrt_price_x96

            if self.tick != _tick:
                updated = True
                self.tick = _tick

            if self.liquidity != _liquidity:
                updated = True
                self.liquidity = _liquidity

            self._update_block = block_number

            if updated:
                self._notify_subscribers(
                    message=UniswapV3PoolStateUpdated(self.state),
                )
                if self._pool_state_archive is not None:  # pragma: no cover
                    self._pool_state_archive[block_number] = self.state

            if not silent:  # pragma: no cover
                logger.info(f"Liquidity: {self.liquidity}")
                logger.info(f"SqrtPriceX96: {self.sqrt_price_x96}")
                logger.info(f"Tick: {self.tick}")

        return updated, self.state

    def calculate_tokens_out_from_tokens_in(
        self,
        token_in: Erc20Token,
        token_in_quantity: int,
        override_state: UniswapV3PoolState | None = None,
    ) -> int:
        """
        This function implements the common degenbot interface `calculate_tokens_out_from_tokens_in`
        to calculate the number of tokens withdrawn (out) for a given number of tokens deposited
        (in).

        It is similar to calling quoteExactInputSingle using the quoter contract with arguments:
        `quoteExactInputSingle(
            tokenIn=token_in,
            tokenOut=[automatically determined by helper],
            fee=[automatically determined by helper],
            amountIn=token_in_quantity,
            sqrt_price_limitX96 = 0
        )` which returns the value `amountOut`

        Note that this wrapper function always assumes that the sqrt_price_limit_x96 argument is
        unset, thus the swap calculation will continue until the target amount is satisfied,
        regardless of price impact.

        Accepts an override of state values.
        """

        if token_in not in self.tokens:  # pragma: no cover
            raise ValueError("token_in not found!")

        if override_state:
            logger.debug(f"V3 calc with overridden state: {override_state}")

        _is_zero_for_one = token_in == self.token0

        try:
            amount0_delta, amount1_delta, *_ = self._calculate_swap(
                zero_for_one=_is_zero_for_one,
                amount_specified=token_in_quantity,
                sqrt_price_limit_x96=(
                    TickMath.MIN_SQRT_RATIO + 1 if _is_zero_for_one else TickMath.MAX_SQRT_RATIO - 1
                ),
                override_state=override_state,
            )
        except EVMRevertError as e:  # pragma: no cover
            raise LiquidityPoolError(f"Simulated execution reverted: {e}") from e
        else:
            return -amount1_delta if _is_zero_for_one else -amount0_delta

    def calculate_tokens_in_from_tokens_out(
        self,
        token_out: Erc20Token,
        token_out_quantity: int,
        override_state: UniswapV3PoolState | None = None,
    ) -> int:
        """
        This function implements the common degenbot interface `calculate_tokens_in_from_tokens_out`
        to calculate the number of tokens deposited (in) for a given number of tokens withdrawn
        (out).

        It is similar to calling quoteExactOutputSingle using the quoter contract with arguments:
        `quoteExactOutputSingle(
            tokenIn=[automatically determined by helper],
            tokenOut=token_out,
            fee=[automatically determined by helper],
            amountOut=token_out_quantity,
            sqrtPriceLimitX96 = 0
        )` which returns the value `amountIn`

        Note that this wrapper function always assumes that the sqrtPriceLimitX96 argument is unset,
        thus the swap calculation will continue until the target amount is satisfied, regardless of
        price impact

        Accepts an override of state values.
        """

        if token_out not in self.tokens:  # pragma: no cover
            raise ValueError("token_out not found!")

        _is_zero_for_one = token_out == self.token1

        try:
            amount0_delta, amount1_delta, *_ = self._calculate_swap(
                zero_for_one=_is_zero_for_one,
                amount_specified=-token_out_quantity,
                sqrt_price_limit_x96=(
                    TickMath.MIN_SQRT_RATIO + 1 if _is_zero_for_one else TickMath.MAX_SQRT_RATIO - 1
                ),
                override_state=override_state,
            )
        except EVMRevertError as e:  # pragma: no cover
            raise LiquidityPoolError(f"Simulated execution reverted: {e}") from e
        else:
            if _is_zero_for_one is True and -amount1_delta < token_out_quantity:
                raise InsufficientAmountOutError(
                    "Insufficient liquidity to swap for the requested amount."
                )
            if _is_zero_for_one is False and -amount0_delta < token_out_quantity:
                raise InsufficientAmountOutError(
                    "Insufficient liquidity to swap for the requested amount."
                )

            return amount0_delta if _is_zero_for_one else amount1_delta

    def external_update(
        self,
        update: UniswapV3PoolExternalUpdate,
    ) -> bool:
        """
        Process a `UniswapV3PoolExternalUpdate` with one or more of the following update types:
            - `block_number`: int
            - `tick`: int
            - `liquidity`: int
            - `sqrt_price_x96`: int
            - `liquidity_change`: tuple of (liquidity_delta, lower_tick, upper_tick). The delta can
                be positive or negative to indicate added or removed liquidity.

        `block_number` is validated against the most recently recorded block prior to recording any
        changes.

        If any update is processed, `self.state` and `self.update_block` are updated.

        Returns a bool indicating whether any updated state value was recorded.

        @dev This method uses a lock to guard state-modifying methods that might cause race
        conditions when used with threads.
        """

        if update.block_number < self.update_block:
            raise ExternalUpdateError(
                f"Rejected update for block {update.block_number} in the past, "
                f"current update block is {self.update_block}"
            )

        with self._state_lock:
            updated_state = False

            if update.tick is not None and update.tick != self.tick:
                updated_state = True
                self.tick = update.tick
                logger.debug(f"Tick: {self.tick}")

            if update.liquidity is not None and update.liquidity != self.liquidity:
                updated_state = True
                self.liquidity = update.liquidity
                logger.debug(f"Liquidity: {self.liquidity}")

            if update.sqrt_price_x96 is not None and update.sqrt_price_x96 != self.sqrt_price_x96:
                updated_state = True
                self.sqrt_price_x96 = update.sqrt_price_x96
                logger.debug(f"SqrtPriceX96: {self.sqrt_price_x96}")

            if update.liquidity_change is not None and update.liquidity_change[0] != 0:
                updated_state = True
                liquidity_delta, lower_tick, upper_tick = update.liquidity_change

                # Adjust in-range liquidity if current tick is within the modified range
                if lower_tick <= self.tick < upper_tick:
                    liquidity_before = self.liquidity
                    self.liquidity += liquidity_delta
                    logger.debug(
                        f"Adjusting in-range liquidity, TX {update.tx}, "
                        f"tick range = [{lower_tick},{upper_tick}], current tick = {self.tick}, "
                        f"{self.address=}, previous liquidity = {liquidity_before}, "
                        f"liquidity change = {liquidity_delta}, new liquidity = {self.liquidity}"
                    )

                for tick in (lower_tick, upper_tick):
                    tick_word, _ = get_tick_word_and_bit_position(tick, self.tick_spacing)

                    if tick_word not in self.tick_bitmap:
                        # The tick bitmap must be known for the word prior to changing the
                        # initialized status of any tick

                        if self.sparse_liquidity_map:
                            logger.debug(
                                f"(external_update) {tick_word=} not found in tick_bitmap."
                            )
                            self._fetch_tick_data_at_word(
                                word_position=tick_word,
                                # Fetch the word using the previous block as a known "good" state
                                # snapshot
                                block_number=update.block_number - 1,
                            )

                        else:
                            # The bitmap is complete (sparse=False), so mark this word as empty
                            self.tick_bitmap[tick_word] = UniswapV3BitmapAtWord()

                    # Get the liquidity info for this tick
                    try:
                        tick_liquidity_net, tick_liquidity_gross = (
                            self.tick_data[tick].liquidityNet,
                            self.tick_data[tick].liquidityGross,
                        )
                    except (KeyError, AttributeError):
                        tick_liquidity_net = 0
                        tick_liquidity_gross = 0
                        TickBitmap.flipTick(
                            self.tick_bitmap,
                            tick,
                            self.tick_spacing,
                            update_block=update.block_number,
                        )

                    # MINT: add liquidity at lower tick (i==0), subtract at upper tick (i==1)
                    # BURN: subtract liquidity at lower tick (i==0), add at upper tick (i==1)
                    # Same equation, but for BURN events the liquidity_delta value is negative
                    new_liquidity_net = (
                        tick_liquidity_net + liquidity_delta
                        if tick == lower_tick
                        else tick_liquidity_net - liquidity_delta
                    )
                    new_liquidity_gross = tick_liquidity_gross + liquidity_delta

                    if new_liquidity_gross == 0:
                        # Delete if there is no remaining liquidity referencing this tick, then
                        # flip it in the bitmap
                        del self.tick_data[tick]
                        TickBitmap.flipTick(
                            self.tick_bitmap,
                            tick,
                            self.tick_spacing,
                            update_block=update.block_number,
                        )
                    else:
                        self.tick_data[tick] = UniswapV3LiquidityAtTick(
                            liquidityNet=new_liquidity_net,
                            liquidityGross=new_liquidity_gross,
                            block=update.block_number,
                        )

                logger.debug(
                    f"liquidity event: {liquidity_delta} in tick range "
                    f"[{lower_tick},{upper_tick}], pool: {self.name}"
                    "\n"
                    f"old liquidity: {tick_liquidity_net} net, {tick_liquidity_gross} gross"
                    "\n"
                    f"new liquidity: {new_liquidity_net} net, {new_liquidity_gross} gross"
                )

            if updated_state:
                if self._pool_state_archive is not None:  # pragma: no branch
                    self._pool_state_archive[update.block_number] = self.state
                self._notify_subscribers(
                    message=UniswapV3PoolStateUpdated(self.state),
                )
                self._update_block = update.block_number

            return updated_state

    def get_absolute_price(
        self,
        token: Erc20Token,
        override_state: UniswapV3PoolState | None = None,
    ) -> Fraction:
        """
        Get the absolute price for the given token, expressed in units of the other.
        """

        return 1 / self.get_absolute_rate(token, override_state=override_state)

    def get_absolute_rate(
        self,
        token: Erc20Token,
        override_state: UniswapV3PoolState | None = None,
    ) -> Fraction:
        """
        Get the absolute rate of exchange for the given token, expressed in units of the other.
        """

        state = self.state if override_state is None else override_state

        if token == self.token0:
            return 1 / exchange_rate_from_sqrt_price_x96(state.sqrt_price_x96)
        elif token == self.token1:
            return exchange_rate_from_sqrt_price_x96(state.sqrt_price_x96)
        else:  # pragma: no cover
            raise ValueError(f"Unknown token {token}")

    def get_fee(self, w3: Web3, block_identifier: BlockIdentifier | None = None) -> int:
        result, *_ = raw_call(
            w3=w3,
            address=self.address,
            calldata=encode_function_calldata(
                function_prototype="fee()",
                function_arguments=None,
            ),
            return_types=["uint256"],
            block_identifier=get_number_for_block_identifier(block_identifier),
        )
        return cast(int, result)

    def get_price_and_tick(
        self, w3: Web3, block_identifier: BlockIdentifier | None = None
    ) -> tuple[int, int]:
        price, tick, *_ = raw_call(
            w3=w3,
            address=self.address,
            calldata=encode_function_calldata(
                function_prototype="slot0()",
                function_arguments=None,
            ),
            return_types=[
                "uint160",
                "int24",
                "uint16",
                "uint16",
                "uint16",
                "uint8",
                "bool",
            ],
            block_identifier=get_number_for_block_identifier(block_identifier),
        )
        return price, tick

    def get_tick_spacing(self, w3: Web3, block_identifier: BlockIdentifier | None = None) -> int:
        result, *_ = raw_call(
            w3=w3,
            address=self.address,
            calldata=encode_function_calldata(
                function_prototype="tickSpacing()",
                function_arguments=None,
            ),
            return_types=["uint256"],
            block_identifier=get_number_for_block_identifier(block_identifier),
        )
        return cast(int, result)

    def get_liquidity(self, w3: Web3, block_identifier: BlockIdentifier | None = None) -> int:
        result, *_ = raw_call(
            w3=w3,
            address=self.address,
            calldata=encode_function_calldata(
                function_prototype="liquidity()",
                function_arguments=None,
            ),
            return_types=["uint256"],
            block_identifier=get_number_for_block_identifier(block_identifier),
        )
        return cast(int, result)

    def get_nominal_price(
        self,
        token: Erc20Token,
        override_state: UniswapV3PoolState | None = None,
    ) -> Fraction:
        """
        Get the nominal price for the given token, expressed in units of the other, corrected for
        decimal place values.
        """
        return 1 / self.get_nominal_rate(token, override_state=override_state)

    def get_nominal_rate(
        self,
        token: Erc20Token,
        override_state: UniswapV3PoolState | None = None,
    ) -> Fraction:
        """
        Get the nominal rate for the given token, expressed in units of the other, corrected for
        decimal place values.
        """

        state = self.state if override_state is None else override_state

        if token == self.token0:
            return (
                1
                / exchange_rate_from_sqrt_price_x96(state.sqrt_price_x96)
                * Fraction(10**self.token1.decimals, 10**self.token0.decimals)
            )
        elif token == self.token1:
            return exchange_rate_from_sqrt_price_x96(state.sqrt_price_x96) * Fraction(
                10**self.token0.decimals, 10**self.token1.decimals
            )
        else:  # pragma: no cover
            raise ValueError(f"Unknown token {token}")

    def discard_states_before_block(self, block: int) -> None:
        """
        Discard states recorded prior to a target block.
        """

        if self._pool_state_archive is None:  # pragma: no cover
            raise NoPoolStateAvailable("No archived states are available")

        with self._state_lock:
            known_blocks = sorted(list(self._pool_state_archive.keys()))

            # Finds the index prior to the requested block number
            block_index = bisect_left(known_blocks, block)

            # The earliest known state already meets the criterion, so return early
            if block_index == 0:
                return

            if block_index == len(known_blocks):
                raise NoPoolStateAvailable(f"No pool state known prior to block {block}")

            for known_block in known_blocks[:block_index]:
                del self._pool_state_archive[known_block]

    def restore_state_before_block(
        self,
        block: int,
    ) -> None:
        """
        Restore the last pool state recorded prior to a target block.

        Use this method to maintain consistent state data following a chain re-organization.
        """

        # Find the index for the most recent pool state PRIOR to the requested
        # block number.
        #
        # e.g. Calling restore_state_before_block(block=104) for a pool with
        # states at blocks 100, 101, 102, 103, 104. `bisect_left()` returns
        # block_index=3, since block 104 is at index=4. The state held at
        # index=3 is for block 103.
        # block_index = self._pool_state_archive.bisect_left(block)

        if self._pool_state_archive is None:  # pragma: no cover
            raise NoPoolStateAvailable("No archived states are available")

        with self._state_lock:
            known_blocks = sorted(list(self._pool_state_archive.keys()))
            block_index = bisect_left(known_blocks, block)

            if block_index == 0:
                raise NoPoolStateAvailable(f"No pool state known prior to block {block}")

            # The last known state already meets the criterion, so return early
            if block_index == len(known_blocks):
                return

            # Remove states at and after the specified block
            for known_block in known_blocks[block_index:]:
                del self._pool_state_archive[known_block]

            self._update_block, self.state = list(self._pool_state_archive.items())[-1]

        self._notify_subscribers(
            message=UniswapV3PoolStateUpdated(self.state),
        )

    def simulate_exact_input_swap(
        self,
        token_in: Erc20Token,
        token_in_quantity: int,
        sqrt_price_limit_x96: int | None = None,
        override_state: UniswapV3PoolState | None = None,
    ) -> UniswapV3PoolSimulationResult:
        """
        Simulate an exact input swap.
        """

        if token_in not in self.tokens:  # pragma: no cover
            raise ValueError("token_in is unknown!")
        if token_in_quantity == 0:  # pragma: no cover
            raise ValueError("Zero input swap requested.")

        zero_for_one = token_in == self.token0

        try:
            (
                amount0_delta,
                amount1_delta,
                end_sqrt_price_x96,
                end_liquidity,
                end_tick,
            ) = self._calculate_swap(
                zero_for_one=zero_for_one,
                amount_specified=token_in_quantity,
                sqrt_price_limit_x96=(
                    sqrt_price_limit_x96
                    if sqrt_price_limit_x96 is not None
                    else (
                        TickMath.MIN_SQRT_RATIO + 1 if zero_for_one else TickMath.MAX_SQRT_RATIO - 1
                    )
                ),
                override_state=override_state,
            )
        except EVMRevertError as e:  # pragma: no cover
            raise LiquidityPoolError(f"Simulated execution reverted: {e}") from e
        else:
            return UniswapV3PoolSimulationResult(
                amount0_delta=amount0_delta,
                amount1_delta=amount1_delta,
                initial_state=self.state.copy(),
                final_state=UniswapV3PoolState(
                    pool=self.address,
                    liquidity=end_liquidity,
                    sqrt_price_x96=end_sqrt_price_x96,
                    tick=end_tick,
                ),
            )

    def simulate_exact_output_swap(
        self,
        token_out: Erc20Token,
        token_out_quantity: int,
        sqrt_price_limit_x96: int | None = None,
        override_state: UniswapV3PoolState | None = None,
    ) -> UniswapV3PoolSimulationResult:
        """
        Simulate an exact output swap.
        """

        if token_out not in self.tokens:  # pragma: no cover
            raise ValueError("token_out is unknown!")
        if token_out_quantity == 0:  # pragma: no cover
            raise ValueError("Zero output swap requested.")

        zero_for_one = token_out == self.token1

        try:
            (
                amount0_delta,
                amount1_delta,
                end_sqrtprice,
                end_liquidity,
                end_tick,
            ) = self._calculate_swap(
                zero_for_one=zero_for_one,
                amount_specified=-token_out_quantity,
                sqrt_price_limit_x96=(
                    sqrt_price_limit_x96
                    if sqrt_price_limit_x96 is not None
                    else (
                        TickMath.MIN_SQRT_RATIO + 1 if zero_for_one else TickMath.MAX_SQRT_RATIO - 1
                    )
                ),
                override_state=override_state,
            )
        except EVMRevertError as e:  # pragma: no cover
            raise LiquidityPoolError(f"Simulated execution reverted: {e}") from e
        else:
            return UniswapV3PoolSimulationResult(
                amount0_delta=amount0_delta,
                amount1_delta=amount1_delta,
                initial_state=self.state.copy(),
                final_state=UniswapV3PoolState(
                    pool=self.address,
                    liquidity=end_liquidity,
                    sqrt_price_x96=end_sqrtprice,
                    tick=end_tick,
                ),
            )


class PancakeV3Pool(V3LiquidityPool):
    def get_price_and_tick(
        self, w3: Web3, block_identifier: BlockIdentifier | None = None
    ) -> tuple[int, int]:
        price, tick, *_ = raw_call(
            w3=w3,
            address=self.address,
            calldata=encode_function_calldata(
                function_prototype="slot0()",
                function_arguments=None,
            ),
            return_types=[
                "uint160",
                "int24",
                "uint16",
                "uint16",
                "uint16",
                "uint32",
                "bool",
            ],
            block_identifier=get_number_for_block_identifier(block_identifier),
        )
        return price, tick


class AerodromeV3Pool(V3LiquidityPool):
    def get_price_and_tick(
        self, w3: Web3, block_identifier: BlockIdentifier | None = None
    ) -> tuple[int, int]:
        price, tick, *_ = raw_call(
            w3=w3,
            address=self.address,
            calldata=encode_function_calldata(
                function_prototype="slot0()",
                function_arguments=None,
            ),
            return_types=[
                "uint160",
                "int24",
                "uint16",
                "uint16",
                "uint16",
                "bool",
            ],
            block_identifier=get_number_for_block_identifier(block_identifier),
        )
        return price, tick

    def _verified_address(self) -> ChecksumAddress:
        # The implementation address is hard-coded into the contract
        implementation_address = to_checksum_address(
            config.get_web3().eth.get_code(self.address)[10:30]
        )

        return generate_aerodrome_v3_pool_address(
            deployer_address=self.deployer_address,
            token_addresses=(self.token0.address, self.token1.address),
            implementation_address=to_checksum_address(implementation_address),
            tick_spacing=self.tick_spacing,
        )

    def get_populated_ticks_in_word(
        self,
        w3: Web3,
        word_position: int,
        block_identifier: BlockIdentifier,
    ) -> list[tuple[int, int, int]]:
        bitmap = self.get_tick_bitmap_at_word(
            w3=w3, word_position=word_position, block_identifier=block_identifier
        )

        populated_ticks = []
        for i in range(256):
            if bitmap & (1 << i) > 0:
                populatedTick = (((word_position) << 8) + (i)) * self.tick_spacing
                liquidityGross, liquidityNet, *_ = raw_call(
                    w3=w3,
                    address=self.address,
                    calldata=encode_function_calldata(
                        function_prototype="ticks(int24)",
                        function_arguments=[populatedTick],
                    ),
                    # AERODROME V3
                    return_types=[
                        "uint128",
                        "int128",
                        "int128",
                        "uint256",
                        "uint256",
                        "uint256",
                        "int56",
                        "uint160",
                        "uint32",
                        "bool",
                    ],
                )
                populated_ticks.append(
                    (
                        populatedTick,
                        liquidityNet,
                        liquidityGross,
                    )
                )

        return populated_ticks
