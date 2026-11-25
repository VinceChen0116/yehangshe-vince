from dataclasses import dataclass, field
import os


@dataclass
class Config:
    source: str = "demo"
    host: str = "127.0.0.1"
    port: int = 8765
    ib_host: str = "127.0.0.1"
    ib_port: int = 4002
    ib_client_id: int = 71
    market_lines: int = 100
    reserve_fraction: float = 0.20
    strikes_each_side: int = 19
    request_rate: float = 20.0
    retirement_seconds: float = 60.0
    queue_size: int = 50000
    spot_timeout: float = 45.0
    greek_timeout: float = 5.0
    vix_enabled: bool = False
    rate: float = 0.04
    dividend: float = 0.012
    spike_multiple: float = 5.0
    feed_min_ask: float = 0.05
    feed_max_ask: float = 4.0
    large_contracts: int = 50
    large_premium: float = 50000.0
    # Matching the supplied brief: illustrative scoring parameters, not probabilities.
    decision_margin: int = 21
    charm_call_sign: int = -1
    charm_put_sign: int = -1
    nightwatch_key: str = field(default="", repr=False)
    massive_spot_key: str = field(default="", repr=False)
    nightwatch_max_units: int = 3000
    nightwatch_flow_stream: bool = False

    @classmethod
    def from_env(cls):
        return cls(
            source=os.getenv("OBSERVER_SOURCE", "demo"),
            port=int(os.getenv("OBSERVER_PORT", "8765")),
            ib_host=os.getenv("IB_HOST", "127.0.0.1"),
            ib_port=int(os.getenv("IB_PORT", "4002")),
            ib_client_id=int(os.getenv("IB_CLIENT_ID", "71")),
            market_lines=int(os.getenv("IB_MARKET_LINES", "100")),
            rate=float(os.getenv("MODEL_RATE", "0.04")),
            dividend=float(os.getenv("MODEL_DIVIDEND", "0.012")),
            vix_enabled=os.getenv("ENABLE_VIX", "0") == "1",
            nightwatch_key=os.getenv("NIGHTWATCH_API_KEY", ""),
            massive_spot_key=os.getenv("MASSIVE_SPOT_API_KEY", "").strip(),
            nightwatch_max_units=int(os.getenv("NIGHTWATCH_MAX_UNITS", "3000")),
            nightwatch_flow_stream=os.getenv("NIGHTWATCH_FLOW_STREAM") == "1",
        )

    @property
    def active_budget(self):
        if self.source == "nightwatch":
            return 11
        return max(0, int(self.market_lines * (1 - self.reserve_fraction)))

    @property
    def option_budget(self):
        return max(0, self.active_budget - 1 - int(self.vix_enabled))

    @property
    def actual_n(self):
        if self.source == "nightwatch":
            return 2
        return max(0, min(self.strikes_each_side, (self.option_budget // 2 - 1) // 2))

    def validate(self):
        if self.source not in {"demo", "ibkr", "nightwatch"}:
            raise ValueError("source must be demo, ibkr or nightwatch")
        if self.nightwatch_max_units < 1:
            raise ValueError("NIGHTWATCH_MAX_UNITS must be positive")
        if self.option_budget < 2:
            raise ValueError("market-line budget cannot fit SPX and one call/put pair")
        if not 0 < self.request_rate <= 45:
            raise ValueError("request_rate must be between 0 and 45")
