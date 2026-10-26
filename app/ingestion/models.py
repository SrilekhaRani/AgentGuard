from datetime import date
from enum import Enum

from pydantic import BaseModel, ConfigDict, field_validator

# Enumerations for source types, environments, and trust levels
class SourceType(str, Enum):
    RUNBOOK = "runbook"
    LOG = "log"

class Environment(str, Enum):
    DEV = "dev"
    STAGING = "staging"
    PROD = "prod"

class TrustLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

# Document
# Import BaseModel, ConfigDict and field_validator from Pydantic, and date from datetime.
# Add model_config = ConfigDict(frozen=True).
# Add the 9 fields from the table: id, source_type, path, team, service, env, text, owner, last_reviewed. The last two are optional with = None.
# Add the two validators: text not blank, and team stripped, lowercased and not blank.

class Document(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    source_type: SourceType
    path: str
    team: str
    service: str
    env: Environment
    text: str
    owner: str | None = None
    last_reviewed: date | None = None

    @field_validator("text")
    @classmethod
    def text_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("text must not be blank")
        return v    

    @field_validator("team", "service")
    @classmethod
    def team_stripped_lowercase_not_blank(cls, v: str) -> str:
        v = v.strip().lower()
        if not v:
            raise ValueError("team must not be blank")
        return v

    
