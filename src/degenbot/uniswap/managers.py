import contextlib
from threading import Lock
from typing import TYPE_CHECKING, Any, Literal

from eth_typing import BlockIdentifier, ChecksumAddress
from eth_utils.address import to_checksum_address
from web3 import Web3

from degenbot.functions import encode_function_calldata, get_number_for_block_identifier, raw_call

from .. import config
from ..constants import ZERO_ADDRESS
from ..exceptions import Erc20TokenError, ManagerError, PoolNotAssociated
from ..exchanges.uniswap.deployments import FACTORY_DEPLOYMENTS
from ..exchanges.uniswap.types import UniswapV2ExchangeDeployment, UniswapV3ExchangeDeployment
from ..logging import logger
from ..manager.token_manager import Erc20TokenHelperManager
from ..registry.all_pools import AllPools
from ..types import AbstractManager
from .v2_liquidity_pool import LiquidityPool
from .v3_functions import generate_v3_pool_address
from .v3_liquidity_pool import V3LiquidityPool
from .v3_snapshot import UniswapV3LiquiditySnapshot


class UniswapLiquidityPoolManager(AbstractManager):
    """
    Single-concern base class to allow derived classes to share state
    """

    _state: dict[int, dict[str, Any]] = dict()

    def __init__(
        self,
        factory_address: str,
        chain_id: int,
    ):
        """
        Initialize the specific state dictionary for the given chain id and
        factory address
        """

        # the internal state data for all child objects is held in a nested
        # class-level dictionary, keyed by chain ID and factory address
        try:
            self._state[chain_id]
        except KeyError:
            self._state[chain_id] = {}
            self._state[chain_id]["erc20token_manager"] = Erc20TokenHelperManager(chain_id)

        try:
            self._state[chain_id][factory_address]
        except KeyError:
            self._state[chain_id][factory_address] = {}


class UniswapV2LiquidityPoolManager(UniswapLiquidityPoolManager):
    """
    A class that generates and tracks Uniswap V2 liquidity pool helpers

    The state dictionary is held using the "Borg" singleton pattern, which
    ensures that all instances of the class have access to the same state data
    """

    @classmethod
    def from_exchange(
        cls,
        exchange: UniswapV2ExchangeDeployment,
    ) -> "UniswapV2LiquidityPoolManager":
        return cls(
            factory_address=exchange.factory.address,
            deployer_address=exchange.factory.deployer,
            pool_init_hash=exchange.factory.pool_init_hash,
            pool_abi=exchange.factory.pool_abi,
        )

    def __init__(
        self,
        factory_address: str,
        deployer_address: ChecksumAddress | str | None = None,
        chain_id: int | None = None,
        pool_init_hash: str | None = None,
        pool_abi: list[Any] | None = None,
    ):
        chain_id = chain_id if chain_id is not None else config.get_web3().eth.chain_id
        factory_address = to_checksum_address(factory_address)

        try:
            factory_deployment = FACTORY_DEPLOYMENTS[chain_id][factory_address]
            deployer_address = (
                factory_deployment.deployer
                if factory_deployment.deployer is not None
                else factory_address
            )
            pool_init_hash = factory_deployment.pool_init_hash
            pool_abi = factory_deployment.pool_abi
        except KeyError:
            if pool_abi is None or pool_init_hash is None:  # pragma: no branch
                raise ManagerError(
                    "Cannot create UniswapV2 pool manager without factory address, pool ABI, and pool init hash."  # noqa:E501
                ) from None
            deployer_address = (
                to_checksum_address(deployer_address)
                if deployer_address is not None
                else factory_address
            )

        super().__init__(
            factory_address=factory_address,
            chain_id=chain_id,
        )

        self.__dict__ = self._state[chain_id][factory_address]

        if self.__dict__ == {}:
            try:
                self._lock = Lock()
                self._chain_id = chain_id
                self._factory_address = factory_address
                self._deployer_address = deployer_address
                self._token_manager: Erc20TokenHelperManager = self._state[chain_id][
                    "erc20token_manager"
                ]
                self._pool_abi = pool_abi
                self._pool_init_hash = pool_init_hash
                self._tracked_pools: dict[ChecksumAddress, LiquidityPool] = dict()
                self._untracked_pools: set[ChecksumAddress] = set()
            except Exception as e:
                self._state[chain_id][factory_address] = {}
                raise ManagerError(f"Could not initialize state for {factory_address}") from e

    def __delitem__(self, pool: LiquidityPool | ChecksumAddress | str) -> None:
        pool_address: ChecksumAddress

        if isinstance(pool, LiquidityPool):
            pool_address = pool.address
        else:
            pool_address = to_checksum_address(pool)

        with contextlib.suppress(KeyError):
            del self._tracked_pools[pool_address]

        self._untracked_pools.discard(pool_address)
        assert pool_address not in self._untracked_pools

    def __repr__(self) -> str:  # pragma: no cover
        return f"UniswapV2LiquidityPoolManager(factory={self._factory_address})"

    def _add_pool(self, pool_helper: LiquidityPool) -> None:
        with self._lock:
            self._tracked_pools[pool_helper.address] = pool_helper
        assert pool_helper.address in self._tracked_pools

    def get_pair_from_factory(
        self,
        w3: Web3,
        token0: ChecksumAddress,
        token1: ChecksumAddress,
        block_identifier: BlockIdentifier | None = None,
    ) -> str:
        pool_address, *_ = raw_call(
            w3=w3,
            address=self._factory_address,
            calldata=encode_function_calldata(
                function_prototype="getPair(address,address)",
                function_arguments=[token0, token1],
            ),
            return_types=["address"],
            block_identifier=get_number_for_block_identifier(block_identifier),
        )
        return pool_address

    def get_pool(
        self,
        pool_address: str | None = None,
        token_addresses: tuple[str, str] | None = None,
        silent: bool = False,
        update_method: Literal["polling", "external"] = "polling",
        state_block: int | None = None,
        liquiditypool_kwargs: dict[str, Any] | None = None,
    ) -> LiquidityPool:
        """
        Get the pool object from its address, or a tuple of token addresses
        """

        if liquiditypool_kwargs is None:
            liquiditypool_kwargs = dict()

        if token_addresses is not None:
            checksummed_token_addresses = tuple(
                [to_checksum_address(token_address) for token_address in token_addresses]
            )

            try:
                for token_address in checksummed_token_addresses:
                    self._token_manager.get_erc20token(
                        address=token_address,
                        silent=silent,
                    )
            except Erc20TokenError:
                raise ManagerError("Could not get both Erc20Token helpers") from None

            pool_address = to_checksum_address(
                self.get_pair_from_factory(
                    w3=config.get_web3(),
                    token0=checksummed_token_addresses[0],
                    token1=checksummed_token_addresses[1],
                    block_identifier=None,
                )
            )
            if pool_address == ZERO_ADDRESS:
                raise ManagerError("No V2 LP available")

        if TYPE_CHECKING:
            assert pool_address is not None
        # Address is now known, check if the pool is already being tracked
        pool_address = to_checksum_address(pool_address)

        if pool_address in self._untracked_pools:
            raise PoolNotAssociated(
                f"Pool address {pool_address} not associated with factory {self._factory_address}"
            )

        try:
            return self._tracked_pools[pool_address]
        except KeyError:
            pass

        # Check if the AllPools collection already has this pool
        pool_helper = AllPools(self._chain_id).get(pool_address)
        if pool_helper:
            if TYPE_CHECKING:
                assert isinstance(pool_helper, LiquidityPool)
            if pool_helper.factory == self._factory_address:
                self._add_pool(pool_helper)
                return pool_helper
            else:
                self._untracked_pools.add(pool_address)
                raise PoolNotAssociated(f"Pool {pool_address} is not associated with this DEX")

        try:
            pool_helper = LiquidityPool(
                address=pool_address,
                silent=silent,
                state_block=state_block,
                # factory_address=self._factory_address,
                # factory_init_hash=self._pool_init_hash,
                update_method=update_method,
                **liquiditypool_kwargs,
            )
        except Exception as exc:
            self._untracked_pools.add(pool_address)
            raise ManagerError(f"Could not build V2 pool {pool_address}: {exc}") from exc
        else:
            self._add_pool(pool_helper)
            return pool_helper


class UniswapV3LiquidityPoolManager(UniswapLiquidityPoolManager):
    """
    A class that generates and tracks Uniswap V3 liquidity pool helpers

    The state dictionary is held using the "Borg" singleton pattern, which
    ensures that all instances of the class have access to the same state data
    """

    @classmethod
    def from_exchange(
        cls,
        exchange: UniswapV3ExchangeDeployment,
        snapshot: UniswapV3LiquiditySnapshot | None = None,
    ) -> "UniswapV3LiquidityPoolManager":
        return cls(
            factory_address=exchange.factory.address,
            deployer_address=exchange.factory.deployer,
            chain_id=exchange.chain_id,
            pool_abi=exchange.factory.pool_abi,
            snapshot=snapshot,
        )

    def __init__(
        self,
        factory_address: ChecksumAddress | str,
        deployer_address: ChecksumAddress | str | None = None,
        chain_id: int | None = None,
        pool_init_hash: str | None = None,
        pool_abi: list[Any] | None = None,
        pool_class: type = V3LiquidityPool,
        snapshot: UniswapV3LiquiditySnapshot | None = None,
    ):
        chain_id = chain_id if chain_id is not None else config.get_web3().eth.chain_id
        factory_address = to_checksum_address(factory_address)

        assert issubclass(pool_class, V3LiquidityPool)

        try:
            factory_deployment = FACTORY_DEPLOYMENTS[chain_id][factory_address]
            deployer_address = (
                factory_deployment.deployer
                if factory_deployment.deployer is not None
                else factory_address
            )
            pool_init_hash = factory_deployment.pool_init_hash
            pool_abi = factory_deployment.pool_abi
        except KeyError:
            if pool_abi is None or pool_init_hash is None:
                raise ManagerError(
                    "Cannot create UniswapV3 pool manager without factory address, pool ABI, and pool init hash."  # noqa:E501
                ) from None
            deployer_address = (
                to_checksum_address(deployer_address)
                if deployer_address is not None
                else factory_address
            )

        super().__init__(
            factory_address=factory_address,
            chain_id=chain_id,
        )

        self.__dict__ = self._state[chain_id][factory_address]

        if self.__dict__ == {}:
            try:
                self._lock = Lock()
                self._chain_id = chain_id
                self._factory_address = factory_address
                self._deployer_address = deployer_address
                self._token_manager: Erc20TokenHelperManager = self._state[chain_id][
                    "erc20token_manager"
                ]
                self._pool_abi = pool_abi
                self._pool_class = pool_class
                self._pool_init_hash = pool_init_hash
                self._snapshot = snapshot
                self._tracked_pools: dict[ChecksumAddress, V3LiquidityPool] = {}
                self._untracked_pools: set[ChecksumAddress] = set()
            except Exception as e:
                self._state[chain_id][factory_address] = {}
                logger.exception("debug")
                raise ManagerError(f"Could not initialize state for {factory_address}") from e

    def __delitem__(self, pool: V3LiquidityPool | ChecksumAddress | str) -> None:
        pool_address: ChecksumAddress

        if isinstance(pool, V3LiquidityPool):
            pool_address = pool.address
        else:
            pool_address = to_checksum_address(pool)

        with contextlib.suppress(KeyError):
            del self._tracked_pools[pool_address]

        self._untracked_pools.discard(pool_address)

    def __repr__(self) -> str:  # pragma: no cover
        return f"UniswapV3LiquidityPoolManager(factory={self._factory_address})"

    def _add_tracked_pool(self, pool_helper: V3LiquidityPool) -> None:
        with self._lock:
            self._tracked_pools[pool_helper.address] = pool_helper

    def _apply_pending_liquidity_updates(self, pool: V3LiquidityPool) -> None:
        """
        Apply all pending updates from the snapshot.
        """

        if not self._snapshot:
            return

        starting_state_block = pool.update_block

        # Apply liquidity modifications
        for i, liquidity_update in enumerate(
            self._snapshot.get_new_liquidity_updates(pool.address)
        ):
            if i == 0:
                pool._update_block = liquidity_update.block_number
            pool.external_update(liquidity_update)

        # Restore the slot0 values state at the original creation block
        pool.auto_update(block_number=starting_state_block)

    def get_pool(
        self,
        pool_address: ChecksumAddress | str | None = None,
        token_addresses: tuple[
            ChecksumAddress | str,
            ChecksumAddress | str,
        ]
        | None = None,
        pool_fee: int | None = None,
        silent: bool = False,
        # keyword arguments passed to the `V3LiquidityPool` constructor
        v3liquiditypool_kwargs: dict[str, Any] | None = None,
        state_block: int | None = None,
    ) -> V3LiquidityPool:
        """
        Get a `V3LiquidityPool` from its address, or a tuple of token addresses and fee in bips
        (e.g. 100, 500, 3000, 10000)
        """

        def find_or_build(
            pool_address: ChecksumAddress,
            state_block: int | None = None,
        ) -> V3LiquidityPool:
            if TYPE_CHECKING:
                assert isinstance(v3liquiditypool_kwargs, dict)

            # Check if the AllPools collection already has this pool
            if pool_helper := AllPools(self._chain_id).get(pool_address):
                if TYPE_CHECKING:
                    assert isinstance(pool_helper, V3LiquidityPool)
                if pool_helper.factory == self._factory_address:
                    self._add_tracked_pool(pool_helper)
                    return pool_helper
                else:
                    self._untracked_pools.add(pool_address)
                    raise PoolNotAssociated(f"Pool {pool_address} is not associated with this DEX")

            if self._snapshot:
                v3liquiditypool_kwargs.update(
                    {
                        "tick_bitmap": self._snapshot.get_tick_bitmap(pool_address),
                        "tick_data": self._snapshot.get_tick_data(pool_address),
                    }
                )
            else:
                logger.info(
                    f"Initializing pool manager at address {self._factory_address} without liquidity snapshot"  # noqa:E501
                )

            # The pool is unknown, so build and add it
            try:
                pool_helper = self._pool_class(
                    address=pool_address,
                    abi=self._pool_abi,
                    silent=silent,
                    state_block=state_block,
                    **v3liquiditypool_kwargs,
                )
            except Exception as e:
                self._untracked_pools.add(pool_address)
                raise ManagerError(f"Could not build V3 pool {pool_address}: {e}") from e
            else:
                self._apply_pending_liquidity_updates(pool_helper)
                self._add_tracked_pool(pool_helper)
                assert isinstance(
                    pool_helper, V3LiquidityPool
                ), f"{self} Attempted to return non-V3 pool {pool_helper}! {pool_address=}, {token_addresses=}, {pool_fee=}"  # noqa:E501
                return pool_helper

        if not (pool_address is None) ^ (token_addresses is None and pool_fee is None):
            raise ValueError("Insufficient arguments provided. Pass address OR tokens & fee")

        if v3liquiditypool_kwargs is None:
            v3liquiditypool_kwargs = dict()

        if pool_address is not None:
            pool_address = to_checksum_address(pool_address)
        elif token_addresses is not None and pool_fee is not None:
            pool_address = generate_v3_pool_address(
                token_addresses=sorted(token_addresses),
                fee=pool_fee,
                deployer_address=self._deployer_address,
                init_hash=self._pool_init_hash,
            )
        else:
            raise ValueError("Provide a pool address or a token address pair and fee")

        if pool_address in self._untracked_pools:
            raise PoolNotAssociated(
                f"Pool address {pool_address} not associated with factory {self._factory_address}"
            )

        try:
            pool_helper = self._tracked_pools[pool_address]
        except KeyError:
            pool_helper = find_or_build(pool_address, state_block)

        if TYPE_CHECKING:
            assert isinstance(pool_helper, V3LiquidityPool)

        assert isinstance(
            pool_helper, V3LiquidityPool
        ), f"{self} Attempted to return non-V3 pool {pool_helper}! {pool_address=}, {token_addresses=}, {pool_fee=}"  # noqa:E501
        return pool_helper
