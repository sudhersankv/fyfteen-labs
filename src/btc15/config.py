from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="KALSHI_", extra="ignore")

    key_id: str = ""
    private_key_path: Path = Path("kalshi-private-key.pem")
    rest_url: str = "https://external-api.kalshi.com/trade-api/v2"
    ws_url: str = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
    series: str = "KXBTC15M"
    db_path: Path = Path("data/paper.db")
    host: str = "127.0.0.1"
    port: int = 8000
    bankroll: float = 100.0
    latency_ms: int = 500
    dollars_per_trade: float = 5.0
    min_dollars_per_trade: float = 1.0
    capital_fraction_per_trade: float = 0.20
    profit_bank_rate: float = 0.20
    max_market_exposure: float = 20.0
    max_total_exposure: float = 25.0
    max_trades_per_market: int = 3
    min_liquidity: float = 2.0
    max_spread: float = 0.12
    stale_seconds: float = 5.0
    min_seconds_remaining: float = 20.0
    cooldown_seconds: float = 20.0
    safety_margin: float = 0.01
    fee_base_rate: float = 0.07
    fee_multiplier: float = 1.0
    balance_precision: float = 0.01
    model_min_training_markets: int = 200
    model_min_confidence: float = 0.20
    model_uncertainty_penalty: float = 1.0
    fractional_kelly: float = 0.25
    flow_half_life_seconds: float = 8.0
    min_valid_btc_target: float = 1000.0

    @property
    def credentials_ready(self) -> bool:
        return bool(self.key_id and self.private_key_path.exists())
