from pydantic import BaseModel, Field

class Relationship(BaseModel):
    from_: str = Field(..., alias="from")
    to: str
    relation: str

    class Config:
        populate_by_name = True

class ExtractionResult(BaseModel):
    concepts: list[str]
    relationships: list[Relationship]
    
class QueryRequest(BaseModel):
    query: str

class RAGResponse(BaseModel):
    answer: str
    citations: list[int]
    citation_label: str
    chunks_used: int
    chunks: list[dict]
    subgraph: dict

class GraphResponse(BaseModel):
    nodes: list[dict]
    edges: list[dict]