from pydantic import BaseModel


class GraphResponse(BaseModel):
	name: str = ""
	nodes: list[dict]
	edges: list[dict]
