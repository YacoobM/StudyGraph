from pydantic import BaseModel


class QueryRequest(BaseModel):
    query: str


class RAGResponse(BaseModel):
    answer: str
    citations: list[int]
    citation_label: str
    chunks_used: int
    chunks: list[dict]
    subgraph: dict