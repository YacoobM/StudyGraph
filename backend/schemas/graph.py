from pydantic import BaseModel


class GraphResponse(BaseModel):
	nodes: list[dict]
	edges: list[dict]
