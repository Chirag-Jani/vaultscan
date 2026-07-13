"""Runtime configuration loaded from environment / .env."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# pre-phd/ (repo root): …/src/vaultscan/config.py → parents[2]
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_DIR = REPO_ROOT / "data"
DEFAULT_RPC_URL = "https://api.mainnet-beta.solana.com"
DEFAULT_CLUSTER = "mainnet"


@dataclass(frozen=True)
class Config:
    rpc_url: str
    cluster: str
    data_dir: Path

    @property
    def programs_path(self) -> Path:
        return self.data_dir / "programs.json"

    @property
    def idls_dir(self) -> Path:
        return self.data_dir / "idls"

    @property
    def idl_index_path(self) -> Path:
        return self.idls_dir / "index.json"

    @property
    def fixtures_dir(self) -> Path:
        return self.data_dir / "fixtures"


def load_config(env_file: Path | None = None) -> Config:
    load_dotenv(env_file or (REPO_ROOT / ".env"))
    data_dir = Path(os.getenv("VAULTSCAN_DATA_DIR", str(DEFAULT_DATA_DIR))).expanduser()
    return Config(
        rpc_url=os.getenv("SOLANA_RPC_URL", DEFAULT_RPC_URL).strip(),
        cluster=os.getenv("SOLANA_CLUSTER", DEFAULT_CLUSTER).strip(),
        data_dir=data_dir,
    )
