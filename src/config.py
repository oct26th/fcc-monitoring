"""Configuration management for FCC Monitor."""
import os
from pathlib import Path
from typing import Optional
from functools import lru_cache

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


# Project root
PROJECT_ROOT = Path(__file__).parent.parent

# Load .env from project root
load_dotenv(PROJECT_ROOT / ".env")


class SpiderCloudConfig(BaseModel):
    """SpiderCloud (spider.cloud) configuration.

    spider.cloud is used as the **network/infrastructure layer** to bypass
    Akamai Bot Manager when scraping apps.fcc.gov.  Set api_key via the
    SPIDERCLOUD_API_KEY environment variable.

    Attributes:
        enabled:      Toggle spider.cloud integration on/off.
        api_key:      Bearer token for the spider.cloud API.
        base_url:     spider.cloud API base URL (rarely needs changing).
        stealth:      Enable stealth mode (JS fingerprint randomisation).
        proxy_enabled: Route through spider.cloud residential proxies.
        render_js:    Render JavaScript before returning HTML.
        timeout:      Per-request timeout in seconds.
        max_retries:  How many times to retry on 429/503 errors.
    """
    enabled:       bool  = True
    api_key:       str   = ""
    base_url:      str   = "https://api.spider.cloud"
    stealth:       bool  = True
    proxy_enabled: bool  = True
    render_js:     bool  = True
    timeout:       float = 60.0
    max_retries:   int   = 3


class BrowserlessConfig(BaseSettings):
    """Browserless API configuration."""
    api_key: str = ""
    region: str = "sfo"

    model_config = SettingsConfigDict(env_prefix="BROWSERLESS_")


class PlaywrightConfig(BaseModel):
    """Playwright configuration."""
    browser: str = "chromium"
    headless: bool = True


class FCCConfig(BaseModel):
    """FCC Direct API configuration."""
    enabled: bool = False
    bulk_download_url: str = ""


class DataSourceConfig(BaseModel):
    """Data source configuration."""
    spidercloud: SpiderCloudConfig = SpiderCloudConfig()
    fcc: FCCConfig = FCCConfig()
    browserless: BrowserlessConfig = BrowserlessConfig()
    playwright: PlaywrightConfig = PlaywrightConfig()


class TelegramConfig(BaseSettings):
    """Telegram notification configuration."""
    bot_token: str = ""
    token: str = ""  # For nested structures like telegram.bot.token
    chat_id: str = ""
    alert_chat_id: Optional[str] = None

    model_config = SettingsConfigDict(env_prefix="TELEGRAM_", extra="allow")


class DiscordConfig(BaseSettings):
    """Discord notification configuration."""
    webhook_url: str = ""

    model_config = SettingsConfigDict(env_prefix="DISCORD_", extra="allow")


class SchedulerConfig(BaseModel):
    """Scheduler configuration."""
    timezone: str = "UTC"
    run_time: str = "14:00"


class DatabaseConfig(BaseModel):
    """Database configuration."""
    path: str = "data/fcc_monitor.db"


class LoggingConfig(BaseModel):
    """Logging configuration."""
    level: str = "INFO"
    file: str = "logs/fcc_monitor.log"


class TargetGrantee(BaseModel):
    """Target company to monitor."""
    name: str
    codes: list[str]


class Settings(BaseSettings):
    """Main application settings."""
    target_grantees: list[TargetGrantee] = []
    data_source: DataSourceConfig = DataSourceConfig()
    telegram: TelegramConfig = TelegramConfig()
    discord: DiscordConfig = DiscordConfig()
    scheduler: SchedulerConfig = SchedulerConfig()
    database: DatabaseConfig = DatabaseConfig()
    logging: LoggingConfig = LoggingConfig()

    model_config = SettingsConfigDict(
        env_prefix="",
        env_nested_delimiter="_",
        extra="allow"
    )


@lru_cache()
def get_settings() -> Settings:
    """Load and cache settings."""
    config_path = PROJECT_ROOT / "config" / "settings.yaml"
    
    if config_path.exists():
        with open(config_path, "r") as f:
            config_data = yaml.safe_load(f)
        
        # Expand environment variables
        config_data = _expand_env_vars(config_data)
        
        return Settings(**config_data)
    
    # Fallback to defaults
    return Settings()


def _expand_env_vars(data):
    """Recursively expand environment variables in config."""
    if isinstance(data, dict):
        return {k: _expand_env_vars(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [_expand_env_vars(item) for item in data]
    elif isinstance(data, str) and data.startswith("${") and data.endswith("}"):
        env_var = data[2:-1]
        return os.getenv(env_var, "")
    return data
