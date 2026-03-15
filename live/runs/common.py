"""Shared launcher helpers for live strategy runners."""
from dataclasses import dataclass
import os

from live.market_metadata import WindowMetadataRegistry
from live.node import build_node, prepare_run_metadata, schedule_stop
from live.order_truth import ProdOrderTruthProvider
from live.sandbox_order import SandboxOrderStore, SandboxOrderTruthProvider
from live.sandbox_wallet import SandboxWalletStore, SandboxWalletTruthProvider
from live.strategies.btc_updown import BtcUpDownConfig, BtcUpDownStrategy
from live.strategies.random_signal import RandomSignalConfig, RandomSignalStrategy
from live.wallet_truth import ProdWalletTruthProvider, make_polymarket_balance_client


@dataclass(frozen=True)
class StrategySpec:
    strategy_cls: type
    config_cls: type


_STRATEGY_SPECS: dict[str, StrategySpec] = {
    "btc_updown": StrategySpec(BtcUpDownStrategy, BtcUpDownConfig),
    "random_signal": StrategySpec(RandomSignalStrategy, RandomSignalConfig),
}

_RESERVED_CONFIG_KEYS = {"pm_instrument_ids", "window_end_times_ns", "outcome_side"}


def strategy_names() -> tuple[str, ...]:
    return tuple(sorted(_STRATEGY_SPECS))


def validate_strategy_config(strategy_name: str, strategy_config: dict[str, object] | None) -> None:
    if strategy_config is None:
        return

    spec = _strategy_spec(strategy_name)
    allowed_keys = set(getattr(spec.config_cls, "__annotations__", {})) - _RESERVED_CONFIG_KEYS
    unknown_keys = sorted(set(strategy_config) - allowed_keys)
    reserved_keys = sorted(set(strategy_config) & _RESERVED_CONFIG_KEYS)

    if reserved_keys:
        joined = ", ".join(reserved_keys)
        raise ValueError(f"Strategy config cannot override reserved runtime keys: {joined}")
    if unknown_keys:
        joined = ", ".join(unknown_keys)
        raise ValueError(f"Unknown {strategy_name} strategy config field(s): {joined}")


def build_strategy(
    strategy_name: str,
    *,
    windows: list[tuple[str, int]],
    outcome_side: str,
    strategy_config: dict[str, object] | None = None,
):
    spec = _strategy_spec(strategy_name)
    validate_strategy_config(strategy_name, strategy_config)

    strategy_kwargs = dict(strategy_config or {})
    strategy_kwargs["pm_instrument_ids"] = tuple(window[0] for window in windows)
    strategy_kwargs["window_end_times_ns"] = tuple(window[1] for window in windows)
    strategy_kwargs["outcome_side"] = outcome_side

    return spec.strategy_cls(spec.config_cls(**strategy_kwargs))


def run_strategy(
    strategy_name: str,
    *,
    slug_pattern: str,
    hours_ahead: int,
    outcome_side: str,
    sandbox: bool,
    binance_us: bool,
    run_secs: int | None,
    strategy_config: dict[str, object] | None = None,
    sandbox_wallet_state_path: str | None = None,
    sandbox_starting_usdc: float | None = None,
) -> None:
    window_metadata = prepare_run_metadata(
        slug_pattern=slug_pattern,
        hours_ahead=hours_ahead,
        outcome_side=outcome_side,
        sandbox=sandbox,
        binance_us=binance_us,
        run_secs=run_secs,
        sandbox_starting_usdc=sandbox_starting_usdc,
    )
    windows = [(window.instrument_id, window.window_end_ns) for window in window_metadata]
    pm_ids = [window[0] for window in windows]
    registry = WindowMetadataRegistry(window_metadata)

    node = build_node(
        pm_ids,
        sandbox=sandbox,
        binance_us=binance_us,
        sandbox_starting_usdc=sandbox_starting_usdc,
    )
    strategy = build_strategy(
        strategy_name,
        windows=windows,
        outcome_side=outcome_side,
        strategy_config=strategy_config,
    )
    _attach_wallet_truth(
        strategy,
        registry=registry,
        sandbox=sandbox,
        sandbox_wallet_state_path=sandbox_wallet_state_path,
        sandbox_starting_usdc=sandbox_starting_usdc,
    )
    _attach_order_truth(strategy, sandbox=sandbox)
    strategy.set_process_stop_callback(node.stop)
    node.trader.add_strategy(strategy)
    node.build()
    timer = schedule_stop(
        lambda: strategy.request_process_stop(
            f"Auto-stop timer elapsed after {run_secs}s"
        ),
        run_secs,
    )
    try:
        node.run()
    finally:
        if timer is not None:
            timer.cancel()


def _strategy_spec(strategy_name: str) -> StrategySpec:
    try:
        return _STRATEGY_SPECS[strategy_name]
    except KeyError as exc:
        known = ", ".join(strategy_names())
        raise ValueError(f"Unknown strategy {strategy_name!r}. Known: {known}") from exc


def _attach_wallet_truth(
    strategy,
    *,
    registry: WindowMetadataRegistry,
    sandbox: bool,
    sandbox_wallet_state_path: str | None,
    sandbox_starting_usdc: float | None,
) -> None:
    if sandbox:
        if sandbox_wallet_state_path is None:
            return
        wallet_address = os.environ["POLYMARKET_TEST_WALLET_ADDRESS"]
        wallet_store = SandboxWalletStore(
            wallet_address=wallet_address,
            collateral_balance=0.0 if sandbox_starting_usdc is None else sandbox_starting_usdc,
            state_path=sandbox_wallet_state_path,
        )
        strategy.set_sandbox_wallet_store(wallet_store)
        strategy.set_wallet_truth_provider(
            SandboxWalletTruthProvider(wallet_store=wallet_store, registry=registry)
        )
        return

    balance_client, wallet_address = make_polymarket_balance_client(sandbox=False)
    strategy.set_wallet_truth_provider(
        ProdWalletTruthProvider(
            wallet_address=wallet_address,
            balance_client=balance_client,
            registry=registry,
        )
    )


def _attach_order_truth(strategy, *, sandbox: bool) -> None:
    if sandbox:
        order_store = SandboxOrderStore()
        strategy.set_sandbox_order_store(order_store)
        strategy.set_order_truth_provider(
            SandboxOrderTruthProvider(order_store=order_store)
        )
        return

    clob_client, _ = make_polymarket_balance_client(sandbox=False)
    strategy.set_order_truth_provider(
        ProdOrderTruthProvider(clob_client=clob_client)
    )
