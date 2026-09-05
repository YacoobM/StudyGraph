from pydantic import BaseModel, Field


class Relationship(BaseModel):
    from_: str = Field(..., alias="from")
    to: str
    relation: str
    summary: str = Field(default="")
    context: str = Field(default="")

    class Config:
        populate_by_name = True


class ExtractionResult(BaseModel):
    concepts: list[str]
    relationships: list[Relationship]