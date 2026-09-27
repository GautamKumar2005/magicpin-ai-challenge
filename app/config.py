import os
from typing import List, Optional
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # --- LLM configuration (Gemini) ---
    GEMINI_API_KEY: str = ""
    MODEL: str = "gemini-flash-latest"
    LLM_TIMEOUT_SECONDS: float = 20.0
    LLM_MAX_TOKENS: int = 500

    # --- Database & Vector DB ---
    PINECONE_API_KEY: Optional[str] = None
    PINECONE_INDEX_NAME: Optional[str] = "magicpin-bot"
    DATABASE_URL: str = "sqlite:///./magicpin.db"
    CORS_ORIGINS: List[str] = ["*"]

    # --- Bot Metadata ---
    TEAM_NAME: str = "Team Vera+"
    TEAM_MEMBERS_RAW: str = "Your Name"
    CONTACT_EMAIL: str = "you@example.com"
    BOT_VERSION: str = "1.0.0"
    APPROACH: str = (
        "LLM composer (Gemini) over the 4-context framework, with deterministic "
        "auto-reply / intent-transition / decline detection driving a state machine, "
        "and a template fallback when the LLM is unavailable."
    )

    # --- Limits ---
    MAX_ACTIONS_PER_TICK: int = 20
    MAX_CONTEXT_PAYLOAD_BYTES: int = 500_000

    @property
    def TEAM_MEMBERS(self) -> List[str]:
        return [m.strip() for m in self.TEAM_MEMBERS_RAW.split(",") if m.strip()]

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

# Active settings instance used by app modules
settings = Settings()

# Backwards compatibility aliases
GEMINI_API_KEY = settings.GEMINI_API_KEY
MODEL = settings.MODEL
LLM_TIMEOUT_SECONDS = settings.LLM_TIMEOUT_SECONDS
LLM_MAX_TOKENS = settings.LLM_MAX_TOKENS
TEAM_NAME = settings.TEAM_NAME
TEAM_MEMBERS = settings.TEAM_MEMBERS
CONTACT_EMAIL = settings.CONTACT_EMAIL
BOT_VERSION = settings.BOT_VERSION
APPROACH = settings.APPROACH
MAX_ACTIONS_PER_TICK = settings.MAX_ACTIONS_PER_TICK
MAX_CONTEXT_PAYLOAD_BYTES = settings.MAX_CONTEXT_PAYLOAD_BYTES