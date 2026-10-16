import json
from dataclasses import asdict, dataclass


@dataclass
class ConnectorError(Exception):
    """One error shape for every tool, so the agent can branch on `code` / `retryable`."""

    code: str  # AUTH_FAILED | FORBIDDEN | NOT_FOUND | RATE_LIMITED | UPSTREAM_ERROR | BAD_REQUEST | INSECURE_TRANSPORT
    message: str
    retryable: bool = False
    retry_after_s: float | None = None
    hint: str | None = None

    def __str__(self) -> str:
        return self.to_json()

    def to_json(self) -> str:
        return json.dumps({"error": {k: v for k, v in asdict(self).items() if v is not None}})
