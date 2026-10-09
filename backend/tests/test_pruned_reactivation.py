import unittest

import numpy as np

from services.graph_engine import StudyGraphEngine


class FakeEmbeddingModel:
    def encode(self, names, show_progress_bar=False):
        vectors = {
            "concept_alpha": [1.0, 0.0],
            "concept alpha": [1.0, 0.0],
            "support": [0.0, 1.0],
        }
        return np.array([vectors[name] for name in names])


class PrunedReactivationTests(unittest.TestCase):
    def make_engine(self):
        # Bypass model loading so this test only exercises cache behavior.
        engine = object.__new__(StudyGraphEngine)
        engine.model = FakeEmbeddingModel()
        engine.graph_status = "active"
        engine.graph_id = None
        engine.concepts = [
            {"name": "concept_alpha", "source": "A", "week": "Week 1", "pages": [1]},
            {"name": "support", "source": "A", "week": "Week 1", "pages": [1]},
            {"name": "support", "source": "A", "week": "Week 1", "pages": [2]},
            {"name": "concept alpha", "source": "B", "week": "Week 2", "pages": [2]},
        ]
        engine.edges = [{
            "from": "concept_alpha",
            "to": "support",
            "relation": "explains",
            "source": "A",
            "chunk_ids": ["a"],
        }]
        engine._doc_concept_names = {"A": {"concept_alpha", "support"}}
        engine.pruned_concept_cache = {}
        engine._reactivated_pruned_keys = set()
        return engine

    def test_matching_active_concept_restores_pruned_context(self):
        engine = self.make_engine()
        engine.prune_low_value_concepts("A", "Week 1", min_mentions=2)

        result = engine.reactivate_similar_pruned_concepts(threshold=0.85)

        self.assertEqual(result["matched"], 1)
        self.assertTrue(any(
            concept.get("reactivated") and concept["source"] == "A"
            for concept in engine.concepts
        ))
        self.assertEqual(len(engine.edges), 1)
        self.assertEqual(engine.edges[0]["from"], "concept alpha")
        self.assertTrue(engine.edges[0]["reactivated"])

    def test_reactivation_is_idempotent(self):
        engine = self.make_engine()
        engine.prune_low_value_concepts("A", "Week 1", min_mentions=2)

        engine.reactivate_similar_pruned_concepts(threshold=0.85)
        result = engine.reactivate_similar_pruned_concepts(threshold=0.85)

        self.assertEqual(result["matched"], 0)
        self.assertEqual(len(engine.concepts), 4)
        self.assertEqual(len(engine.edges), 1)

    def test_dissimilar_cached_concept_stays_dormant(self):
        engine = self.make_engine()
        engine.prune_low_value_concepts("A", "Week 1", min_mentions=2)
        engine.concepts = [concept for concept in engine.concepts if concept["source"] != "B"]
        engine.concepts.append({
            "name": "support",
            "source": "B",
            "week": "Week 2",
            "pages": [2],
        })

        result = engine.reactivate_similar_pruned_concepts(threshold=0.85)

        self.assertEqual(result["matched"], 0)
        self.assertEqual(len(engine.edges), 0)


if __name__ == "__main__":
    unittest.main()
