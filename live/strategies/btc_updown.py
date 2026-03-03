"""BTC up/down market strategy — same class runs in backtest and live."""
from collections import deque

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Quantity
from nautilus_trader.trading.strategy import Strategy


class BtcUpDownConfig(StrategyConfig, frozen=True):
    pm_instrument_id: str           # "TOKEN_ID.POLYMARKET"
    btc_bar_type: str = "BTCUSDT.BINANCE-1-MINUTE-LAST-EXTERNAL"
    interval_minutes: int = 15
    trade_amount_usdc: float = 5.0
    signal_lookback: int = 5        # N bars of BTC momentum


class BtcUpDownStrategy(Strategy):
    def __init__(self, config: BtcUpDownConfig):
        super().__init__(config)
        self._pm_instrument_id = InstrumentId.from_str(config.pm_instrument_id)
        self._btc_bar_type = BarType.from_str(config.btc_bar_type)
        self._pm_bar_type = BarType.from_str(
            f"{config.pm_instrument_id}-1-MINUTE-LAST-EXTERNAL"
        )
        self._interval_ns = config.interval_minutes * 60 * 1_000_000_000
        self._trade_amount = config.trade_amount_usdc
        self._signal_lookback = config.signal_lookback

        self._btc_closes: deque[float] = deque(maxlen=config.signal_lookback + 1)
        self._entered_this_epoch = False
        self._current_epoch = -1
        self._trade_count = 0

    def on_start(self):
        self.subscribe_bars(self._btc_bar_type)
        self.subscribe_bars(self._pm_bar_type)
        self.log.info(
            f"Started | PM={self._pm_instrument_id} "
            f"lookback={self._signal_lookback} amount=${self._trade_amount}"
        )

    def on_bar(self, bar: Bar):
        if bar.bar_type.instrument_id.venue.value == "BINANCE":
            self._on_btc_bar(bar)
        else:
            self._on_pm_bar(bar)

    def _on_btc_bar(self, bar: Bar):
        self._btc_closes.append(float(bar.close))

    def _on_pm_bar(self, bar: Bar):
        epoch = bar.ts_event // self._interval_ns
        if epoch != self._current_epoch:
            self._current_epoch = epoch
            self._entered_this_epoch = False
            # Close any leftover position from the previous epoch
            for pos in self.cache.positions_open(instrument_id=self._pm_instrument_id):
                self.close_position(pos)

        if len(self._btc_closes) <= self._signal_lookback:
            return

        signal = self._compute_signal()
        positions = self.cache.positions_open(instrument_id=self._pm_instrument_id)

        # Exit: close long on bearish signal
        if positions and signal == -1:
            for pos in positions:
                self.close_position(pos)
            return

        # Enter: buy YES on bullish signal, once per epoch
        if not positions and not self._entered_this_epoch and signal == 1:
            self._submit_yes_order()
            self._entered_this_epoch = True

    def _compute_signal(self) -> int:
        """1=bullish, -1=bearish, 0=neutral."""
        closes = list(self._btc_closes)
        if closes[-1] > closes[0]:
            return 1
        elif closes[-1] < closes[0]:
            return -1
        return 0

    def _submit_yes_order(self):
        order = self.order_factory.market(
            instrument_id=self._pm_instrument_id,
            order_side=OrderSide.BUY,
            quantity=Quantity.from_str(f"{self._trade_amount:.6f}"),
        )
        self.submit_order(order)

    def on_order_filled(self, event):
        self._trade_count += 1
        self.log.info(
            f"Fill #{self._trade_count}: price={event.last_px} qty={event.last_qty}"
        )

    def on_stop(self):
        self.log.info(f"Stopped | total fills: {self._trade_count}")
