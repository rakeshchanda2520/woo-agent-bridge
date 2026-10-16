from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All config comes from env vars, then .env, then the demo's generated keys. Secrets are SecretStr.

    pydantic-settings lets LATER env files win, so the demo keys go first: a reviewer's own .env must
    always beat them, never the other way round.
    """

    model_config = SettingsConfigDict(env_prefix="WOO_", env_file=("docker/out/keys.env", ".env"), extra="ignore")

    base_url: str = Field(description="Store root, e.g. https://shop.example.com")
    consumer_key: SecretStr
    consumer_secret: SecretStr
    ca_bundle: str | None = Field(None, description="Custom CA/cert for self-signed stores (local demo)")
    allow_insecure_http: bool = Field(
        False, description="Allow plain-http stores. Keys then travel in the query string. Demo only."
    )

    # Politeness towards the merchant's server. Defaults sit under typical shared-hosting WAF limits.
    rate_per_sec: float = 4.0
    burst: int = 4
    max_concurrency: int = 4
    max_retries: int = 4
    timeout_s: float = 15.0

    # Privacy: masking is a deployment decision, never a per-call argument the agent can flip.
    mask_pii: bool = True

    # Payment-id lookup has no server-side filter in WooCommerce, so we scan recent orders.
    payment_scan_max_orders: int = 300
