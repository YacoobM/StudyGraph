from pydantic import BaseModel, Field, field_validator


class GraphCreateRequest(BaseModel):
	name: str

	@field_validator("name")
	@classmethod
	def normalize_name(cls, name: str) -> str:
		name = name.strip()
		if not name:
			raise ValueError("Graph name cannot be empty.")
		return name


class GraphVersionCreateRequest(BaseModel):
	document_ids: list[str] = Field(min_length=1)
	max_nodes_per_document: int | None = Field(default=None, ge=1)
	min_mentions: int = Field(default=1, ge=1)


class GraphResponse(BaseModel):
	name: str = ""
	nodes: list[dict]
	edges: list[dict]
	status: str = "active"
