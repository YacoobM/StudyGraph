from typing import Dict, Literal, Optional
from pydantic import AliasChoices, BaseModel, Field


class QuizSubmission(BaseModel):
    """Request body for POST /api/quiz/{quiz_id}/submit: question id to chosen option index."""
    answers: Dict[str, int]


class QuizQuestion(BaseModel):
    """
    One relationship quiz question, matching quiz_data.build_relationship_question's output.
    """
    id: str
    type: str
    question: str
    options: list[str]
    correct_index: int
    explanation: str
    source: str


class QuizResponse(BaseModel):
    """
    Response body for GET /api/quiz/{scope}.
    """
    quiz_id: str
    questions: list[QuizQuestion]


class QuizAttempt(BaseModel):
    """One finished attempt for POST /api/quiz/results. The frontend sends quiz_id; topic_id is
    still accepted."""
    quiz_id: str = Field(validation_alias=AliasChoices("quiz_id", "topic_id"), min_length=1)
    kind: Literal["document", "section"] = "document"
    answers: list[Optional[int]] = Field(default_factory=list)
    correct: int = Field(ge=0)
    total: int
    title: Optional[str] = None      # shown in the Quiz tab's lists
    group: Optional[str] = None      # a section quiz's note title


TopicResult = QuizAttempt