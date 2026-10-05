from pydantic import BaseModel, Field


class Relationship(BaseModel):
    from_: str = Field(..., alias="from")
    to: str
    relation: str
    chunk_id: str
    summary: str = Field(default="")

    class Config:
        populate_by_name = True


class Concept(BaseModel):
    name: str
    description: str = ""


class ExtractionResult(BaseModel):
    graph_name: str = ""
    # Keep accepting the legacy string form while allowing the newer,
    # description-carrying object form from the extraction prompt.
    concepts: list[Concept | str]
    relationships: list[Relationship]
