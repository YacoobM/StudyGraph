from typing import Optional

from pydantic import BaseModel


class DocumentPatch(BaseModel):
	status: Optional[str] = None
	week: Optional[str] = None
	concepts: Optional[int] = None
	relationships: Optional[int] = None
	error: Optional[str] = None
