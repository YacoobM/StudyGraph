from typing import Dict
from pydantic import BaseModel


class QuizSubmission(BaseModel):
    """
    Request body for POST /api/quiz/{quiz_id}/submit.

    answers maps question_id -> the selected option index.
    """
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


class TopicResult(BaseModel):
    topic_id: str
    correct: int
    total: int
    points: int