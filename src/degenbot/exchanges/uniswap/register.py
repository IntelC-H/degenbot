from .deployments import FACTORY_DEPLOYMENTS, ROUTER_DEPLOYMENTS
from .types import (
    UniswapRouterDeployment,
    UniswapV2ExchangeDeployment,
    UniswapV3ExchangeDeployment,
)


def register_exchange(exchange: UniswapV2ExchangeDeployment | UniswapV3ExchangeDeployment) -> None:
    if exchange.chain_id not in FACTORY_DEPLOYMENTS:
        FACTORY_DEPLOYMENTS[exchange.chain_id] = {}

    if exchange.factory.address in FACTORY_DEPLOYMENTS[exchange.chain_id]:
        raise ValueError("Exchange is already registered.")

    FACTORY_DEPLOYMENTS[exchange.chain_id][exchange.factory.address] = exchange.factory


def register_router(router: UniswapRouterDeployment) -> None:
    if router.chain_id not in ROUTER_DEPLOYMENTS:
        ROUTER_DEPLOYMENTS[router.chain_id] = {}

    if router.address in ROUTER_DEPLOYMENTS[router.chain_id]:
        raise ValueError("Router is already registered.")

    ROUTER_DEPLOYMENTS[router.chain_id][router.address] = router
