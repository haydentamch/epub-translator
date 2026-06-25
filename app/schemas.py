from pydantic import BaseModel, Field


class OpenRouterKeyRequest(BaseModel):
    api_key: str = Field(min_length=1)

