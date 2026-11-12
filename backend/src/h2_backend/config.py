from dataclasses import dataclass
from pathlib import Path
import os


@dataclass(frozen=True)
class Settings:
    database_url: str
    gateway_address: str
    data_dir: Path
    command_ttl_ms: int = 30_000

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_url=os.getenv(
                "H2_DATABASE_URL",
                "postgresql+psycopg://h2:h2_validation@127.0.0.1:5432/h2_validation",
            ),
            gateway_address=os.getenv("H2_GATEWAY_ADDRESS", "127.0.0.1:50051"),
            data_dir=Path(os.getenv("H2_DATA_DIR", ".local/backend-data")).resolve(),
            command_ttl_ms=int(os.getenv("H2_COMMAND_TTL_MS", "30000")),
        )
