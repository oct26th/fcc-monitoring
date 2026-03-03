"""Configuration management for FCC Monitor."""
import os
from pathlib import Path
from typing import Optional
from functools import lru_cache

import yaml
from pydantic import BaseModel
from pydantic_settings import BaseSettings


# Project root
PROJECT_ROOT = Path(__file__).parent.parent


class SpiderCloudConfig(BaseModel):
    """SpiderCloud API configuration."""
    enabled: bool = True
    api_key: str = ""
    base_url: str = "https://api.spidercloud.com/v1"


class FCCConfig(BaseModel):
    """FCC Direct API configuration."""
    enabled: bool = False
    bulk_download_url: str = ""


class DataSourceConfig(BaseModel):
    """Data source configuration."""
    spidercloud: SpiderCloudConfig = SpiderCloudConfig()
    fcc: FCCConfig = FCCConfig()


class TelegramConfig(BaseModel):
    """Telegram notification configuration."""
    bot_token: str = ""
    chat_id: str = ""
    alert_chat_id: Optional[str] = None


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
    scheduler: SchedulerConfig = SchedulerConfig()
    database: DatabaseConfig = DatabaseConfig()
    logging: LoggingConfig = LoggingConfig()

    class Config:
        env_prefix = ""
        env_nested_delimiter = "_"


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
