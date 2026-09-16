"""Application settings, loaded from environment / .env."""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- database -------------------------------------------------------
    # PostgreSQL is the target store. DB_URL_FALLBACK lets the POC still run
    # end-to-end on a machine without a Postgres instance; every run records
    # which backend it actually used.
    database_url: str = "postgresql+psycopg://poc:poc@localhost:5432/uk_election"
    database_url_fallback: str = f"sqlite:///{(ROOT / 'data' / 'uk_election.sqlite3').as_posix()}"
    allow_sqlite_fallback: bool = True

    # --- http client ----------------------------------------------------
    http_timeout: float = 60.0
    http_retries: int = 4
    http_backoff_base: float = 0.8
    http_concurrency: int = 3
    http_delay_seconds: float = 0.4
    user_agent: str = (
        "uttarakhand-election-poc/0.1 (research; contact: set CONTACT_EMAIL in .env)"
    )

    # --- paths ----------------------------------------------------------
    data_dir: Path = ROOT / "data"
    raw_dir: Path = ROOT / "data" / "raw"
    processed_dir: Path = ROOT / "data" / "processed"
    research_dir: Path = ROOT / "research"
    sources_config: Path = ROOT / "config" / "sources.yaml"

    # --- ocr ------------------------------------------------------------
    tesseract_cmd: str | None = None
    # Language data directory. The default Windows install ships English only; the
    # Hindi model is kept in the project so no admin rights are needed.
    tessdata_dir: Path | None = ROOT / "data" / "ocr" / "tessdata"
    ocr_languages: str = "hin+eng"
    ocr_dpi: int = 300

    # --- provenance -----------------------------------------------------
    parser_version: str = "1.0.0"

    @property
    def root(self) -> Path:
        return ROOT


settings = Settings()
