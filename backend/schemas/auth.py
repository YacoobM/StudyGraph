from pydantic import BaseModel, Field


class SignupRequest(BaseModel):
	email: str
	password: str
	name: str = Field(default="")


class LoginRequest(BaseModel):
	email: str
	password: str
