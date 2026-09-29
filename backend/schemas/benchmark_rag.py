from pydantic import BaseModel

class RagasResult(BaseModel):
    """RAGAS evaluation result for a single sample."""
    query: str
    generated_answer: str
    ground_truth: str
    answer_relevancy: float
    faithfulness: float
    answer_similarity: float
    context_recall: float
    overall_score: float | None = None
