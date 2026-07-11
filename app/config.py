"""Settings from .env. One source of truth; providers read keys from here."""
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    openlease_password: str = "changeme"
    secret_key: str = ""
    session_https_only: bool = False  # set true when serving over HTTPS (adds Secure to the cookie)

    # keys — every one optional; blank = that unlock is off, the app still runs
    anthropic_api_key: str = ""
    llm_model: str = "claude-opus-4-8"  # or claude-haiku-4-5 for cheaper parsing
    voyage_api_key: str = ""            # semantic ranking; blank = pure BM25
    google_maps_key: str = ""           # Street View embed only

    # crawler
    crawl_user_agent: str = (
        "OpenLeaseBot/0.1 (+https://github.com/fintok/openlease; openlease@example.com) "
        "self-hosted single-user"
    )
    crawl_delay_seconds: float = 4.0    # 1 req / 3-5s per domain (spec floor)
    crawl_daily_cap_per_domain: int = 500
    crawl_stealth: bool = True          # ON by default (spec); needs `scrapling install`

    overpass_url: str = "https://overpass-api.de/api/interpreter"
    osrm_url: str = "https://router.project-osrm.org"

    monthly_budget_cents: int = 1000
    db_path: str = "openlease.db"

    @field_validator("*", mode="before")
    @classmethod
    def _drop_inline_comment(cls, v, info):
        """`KEY=   # note` in .env yields the comment as the value: python-dotenv only
        strips an inline comment when the value is non-empty. Left alone, a blank
        SECRET_KEY reads as truthy and app.py never generates a random one."""
        if isinstance(v, str) and v.lstrip().startswith("#"):
            return cls.model_fields[info.field_name].default
        return v


settings = Settings()


if __name__ == "__main__":  # python -m app.config
    import os
    os.environ |= {"SECRET_KEY": "  # leave blank -> generated", "MONTHLY_BUDGET_CENTS": "250"}
    s = Settings(_env_file=None)
    assert s.secret_key == "", f"comment leaked into secret_key: {s.secret_key!r}"
    assert s.monthly_budget_cents == 250, s.monthly_budget_cents
    assert Settings(_env_file=None, voyage_api_key="abc123").voyage_api_key == "abc123"
    print("config ok — inline comments dropped, real values preserved")
