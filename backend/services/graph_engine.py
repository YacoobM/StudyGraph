import asyncio
import hashlib
import json
import time
from collections import Counter, defaultdict

import numpy as np
from sklearn.metrics.pairwise import cosine_similarity

from app.config import EXTRACTION_MODEL, RAG_MODEL
from services import graph_fusion
from services.graph_runtime import call_llm
from sklearn.metrics.pairwise import cosine_similarity
from services.embeddings import encode_texts

from schemas.extraction import ExtractionResult
from app.config import (
    EXTRACTION_MAX_COMPLETION_TOKENS,
    EXTRACTION_MODEL,
    RAG_MODEL,
)

from utils.rag_utils import build_rag_system_prompt, build_rag_user_prompt, call_rag_llm, filter_sources_by_chunks_used, prioritize_graph


from app.logging_config import get_logger
from services import graph_fusion
from services.llm_provider import API_STATUS_ERRORS, RATE_LIMIT_ERRORS
from services.graph_runtime import call_llm

ENGINE_EXTRACTION_SEMAPHORE = asyncio.Semaphore(10)
CROSS_WEEK_CONCEPT_COUNT = 10
DEFAULT_CHUNK_SIZE = 1024
DEFAULT_CHUNK_OVERLAP = 150
logger = get_logger(__name__)

PRUNED_REACTIVATION_SIMILARITY = 0.85


class StudyGraphEngine:
    def __init__(self, collections, client, llm_model=EXTRACTION_MODEL, rag_model=RAG_MODEL, graph_id=None):
        self.client = client
        self.llm_model = llm_model
        self.rag_model = rag_model
        self.graph_id = graph_id
        self.graph_name = ""
        self.concepts = []
        self.edges = []
        self.chunk_store = []
        self.graph = None
        self.graph_status = "active"
        self.canonical_nodes = {}       # Map from variants to canonical names (post-fusion)
        self.fused_edges = []           # Edges after conflict resolution (post-fusion)
        self.inferred_edges = []        # Transitive edges inferred during fusion
        self.upload_latency = None              # float, total time for entire upload endpoint
        self.collections = collections
        self._doc_concept_names = (
            {}
        )  # doc_name -> set of admitted concept names (for max_nodes_per_doc)
        self._chroma_chunk_counter = (
            0  # monotonic counter so chunk ids never collide across uploads
        )
        self.pruned_concept_cache = {}  # key -> dormant pruned concept metadata
        self._reactivated_pruned_keys = (
            set()
        )  # keys already restored to avoid duplicate reactivation

        # --- Concept-evolution state ---
        self.week_counter = 1  # Tracks upload batches as "weeks"
        self.uploads_by_week = {}  # week_key -> filename
        self.chunks_by_week = defaultdict(
            list
        )  # week_key -> [{chunk_id, text, pages, doc_name, embedding}]
        self.chunks_by_id = {}  # chunk_id -> chunk provenance record

        # Concept names admitted from completed uploads, across all weeks.
        # Populated after each upload (see _run_upload_pipeline) and consulted
        # by extract_knowledge for later uploads, so a concept established in
        # an earlier week can still be extracted from a later document even if
        # it isn't among that document's own top seed entities.
        self.known_concepts = set()
        self.known_concept_embeddings = (
            None  # np.ndarray aligned with sorted(known_concepts), rebuilt on update
        )
        self._known_concepts_list = (
            []
        )  # sorted(known_concepts), kept alongside known_concept_embeddings

        self.concept_week_chunks = {}  # (name, week) -> retrieved chunk list
        self.concept_week_summaries = {}  # (name, week) -> LLM summary text
        self.concept_consecutive_comparisons = (
            {}
        )  # canonical concept_name -> [{from_week, to_week, comparison}]
        self.concept_overall_comparison = (
            {}
        )  # canonical concept_name -> comparison text (first week vs. latest week)

        self.pipeline_status = {"stage": "idle"}
        self.failed_upload_batches = {}

        self.degraded_summary_keys = set()
        self.quiz_sessions = {}

    def _deduplicate_seeds_semantically(self, seeds, threshold=0.85):
        """
        Remove semantically redundant seeds using cosine similarity.
        Prevents variants like 'deep fake', 'deep fakes', 'deepfake' all surviving.
        """
        if not seeds:
            return seeds
        embeddings = np.asarray(encode_texts(seeds))
        kept = []
        kept_embeddings = []
        for i, seed in enumerate(seeds):
            if not kept_embeddings:
                kept.append(seed)
                kept_embeddings.append(embeddings[i])
                continue
            sims = cosine_similarity([embeddings[i]], kept_embeddings)[0]
            if max(sims) < threshold:
                kept.append(seed)
                kept_embeddings.append(embeddings[i])
        logger.info(f"Semantic deduplication: {len(seeds)} → {len(kept)} seeds")
        return kept

    def get_chunks(
        self,
        pages,
        source="unknown",
        size=DEFAULT_CHUNK_SIZE,
        overlap=DEFAULT_CHUNK_OVERLAP,
        week_key=None,
    ):
        chunks = []

        for p in pages:
            if not isinstance(p, dict):
                continue

            page_num = p.get("page")
            page_text = p.get("text", "")

            if page_num is None or not isinstance(page_text, str):
                continue

            if not page_text.strip():
                continue

            step = max(size - overlap, 1)
            start = 0
            while start < len(page_text):
                end = min(start + size, len(page_text))
                chunk_text = page_text[start:end]
                chunk_index = len(chunks)
                digest = hashlib.sha256(chunk_text.encode("utf-8")).hexdigest()[:12]
                chunk_id = f"{source}:chunk:{chunk_index}:{digest}"
                chunks.append(
                    {
                        "chunk_id": chunk_id,
                        "text": chunk_text,
                        "pages": [page_num],
                        "source": source,
                        "index": chunk_index,
                    }
                )
                start += step

        self.index_chunks(chunks, week_key=week_key)

        if week_key is not None:
            self.index_chunks_for_week(chunks, week_key, source)

        for chunk in chunks:
            self.chunks_by_id[chunk["chunk_id"]] = {
                "chunk_id": chunk["chunk_id"],
                "text": chunk["text"],
                "pages": chunk["pages"],
                "source": source,
                "week": week_key,
            }

        return chunks

    def resolve_chunk(self, chunk_id):
        """Return stored source material for an extracted provenance ID."""
        chunk = self.chunks_by_id.get(chunk_id)
        if not chunk:
            return None
        return {
            "chunk_id": chunk["chunk_id"],
            "text": chunk["text"],
            "pages": list(chunk.get("pages") or []),
            "source": chunk.get("source", "unknown"),
            "week": chunk.get("week"),
        }

    def rebuild_known_concept_embeddings(self):
        """Recompute the embedding index for known_concepts. Call after
        known_concepts changes, before relevant_known_concepts is used."""
        self._known_concepts_list = sorted(self.known_concepts)
        if self._known_concepts_list:
            self.known_concept_embeddings = np.asarray(
                encode_texts(self._known_concepts_list)
            )
        else:
            self.known_concept_embeddings = None

    def relevant_known_concepts(self, text, top_k=CROSS_WEEK_CONCEPT_COUNT):
        """Known concepts (from prior weeks) most relevant to `text`, ranked
        by embedding similarity rather than a flat alphabetical slice."""
        if not self._known_concepts_list or self.known_concept_embeddings is None:
            return []
        query_embedding = np.asarray(encode_texts([text[:2000].lower()]))
        sims = cosine_similarity(query_embedding, self.known_concept_embeddings)[0]
        top_idx = np.argsort(sims)[::-1][:top_k]
        return [self._known_concepts_list[i] for i in top_idx]

    async def extract_knowledge(self, text_batch, known_concepts=None):
        chunk = {
            "chunk_id": "legacy:chunk:0",
            "text": text_batch,
        }
        result = await self.extract_knowledge_batch(
            [chunk], {chunk["chunk_id"]: known_concepts or []}
        )
        return result

    async def extract_knowledge_batch(self, chunks, known_concepts_by_chunk=None):
        raw_data = None  # so except blocks can always reference it
        known_concepts_by_chunk = known_concepts_by_chunk or {}

        priority_line = (
            "Extract specific academic concepts and direct relationships from the text.\n"
            "CRITICAL RULES FOR ENTITY SELECTION:\n"
            "1. Do NOT extract overly broad umbrella terms, document titles, chapter headers, or meta-categories "
            "(e.g., 'introduction', 'overview', 'fundamentals', 'key concepts', 'main subjects', or the overall topic name of the document itself).\n"
            "2. Extract ONLY specific, granular concepts (e.g., specific algorithms, mechanisms, core functions, formal theories, or distinct components).\n"
        )

        known_lines = []
        for chunk in chunks:
            known_concepts = known_concepts_by_chunk.get(chunk["chunk_id"], [])
            if known_concepts:
                known_lines.append(
                    f"For {chunk['chunk_id']}, preserve these known names if referenced: "
                    f"{', '.join(known_concepts)}."
                )
        known_line = "\n".join(known_lines)

        prompt = f"""You are a JSON-only extraction engine. You must respond with ONLY a valid JSON object and absolutely nothing else — no explanation, no markdown, no code fences.

{priority_line}{known_line}
Relationship types: prerequisite, causes, part_of, explains, enables, related_to
Every relationship must include the chunk_id of the source chunk. Do not reproduce quotes or context text.
For each relationship, include:
- summary: a short explanation (one sentence max)
For each concept, include:
- name: the specific academic concept
- description: a concise 1-2 sentence explanation grounded only in the supplied text

Respond with ONLY this exact JSON structure:
{{"graph_name" : "A short-descriptive title of the graph","concepts": [{{"name": "concept1", "description": "Concise grounded explanation."}}, {{"name": "concept2", "description": "Concise grounded explanation."}}], "relationships": [{{"from": "concept1", "to": "concept2", "relation": "enables", "chunk_id": "source:chunk:0:hash", "summary": "Short explanation."}}]}}"""

        user_text = "\n\n---\n\n".join(
            f"CHUNK_ID: {chunk['chunk_id']}\nTEXT:\n{chunk['text']}" for chunk in chunks
        )

        try:
            semaphore_started = time.perf_counter()
            logger.info(
                "concurrency_trace stage=extraction_wait graph_id=%s chunks=%d semaphore_locked=%s limit=10",
                self.graph_id, len(chunks), ENGINE_EXTRACTION_SEMAPHORE.locked(),
            )
            async with ENGINE_EXTRACTION_SEMAPHORE:
                logger.info(
                    "concurrency_trace stage=extraction_acquired graph_id=%s wait_ms=%.1f",
                    self.graph_id, (time.perf_counter() - semaphore_started) * 1000,
                )
                response = await call_llm(
                    self.client,
                    self.llm_model,
                    prompt,
                    user_text,
                    EXTRACTION_MAX_COMPLETION_TOKENS,
                )
                logger.info(
                    "concurrency_trace stage=extraction_finished graph_id=%s held_ms=%.1f",
                    self.graph_id, (time.perf_counter() - semaphore_started) * 1000,
                )
            content = response.choices[0].message.content

            if not content or not content.strip():
                logger.warning("chunk_extraction_empty_response")
                return {"concepts": [], "relationships": []}

            # Strip markdown fences if the model wraps output anyway
            content = content.strip()
            if content.startswith("```"):
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
                content = content.strip()

            raw_data = json.loads(content)
            validated = ExtractionResult(**raw_data)
            valid_chunk_ids = {chunk["chunk_id"] for chunk in chunks}
            invalid_relationships = [
                relationship
                for relationship in validated.relationships
                if relationship.chunk_id not in valid_chunk_ids
            ]
            if invalid_relationships:
                raise ValueError(
                    "LLM returned relationship provenance for an unknown chunk_id"
                )
            return {
                "graph_name": validated.graph_name,
                "concepts": [
                    {
                        "name": concept.name,
                        "description": concept.description,
                    }
                    for concept in validated.concepts
                ],
                "relationships": [
                    {
                        "from": r.from_,
                        "to": r.to,
                        "relation": r.relation,
                        "summary": getattr(r, "summary", "") or "",
                        "chunk_id": r.chunk_id,
                    }
                    for r in validated.relationships
                ],
            }
        except ValueError as e:
            logger.warning(
                "chunk_extraction_validation_failed error=%s raw_data_present=%s",
                e,
                raw_data is not None,
            )
            return {"concepts": [], "relationships": []}
        except json.JSONDecodeError as e:
            logger.warning("chunk_extraction_json_failed error=%s", e)
            return {"concepts": [], "relationships": []}
        except RATE_LIMIT_ERRORS as e:
            logger.warning("chunk_extraction_rate_limited error=%s", e)
            raise
        except API_STATUS_ERRORS:
            # Do not turn invalid model IDs, credentials, or other API errors
            # into a misleading empty-but-successful extraction.
            raise
        except Exception as e:
            logger.exception(
                "chunk_extraction_failed raw_data_present=%s", raw_data is not None
            )
            return {"concepts": [], "relationships": []}

    def _mark_graph_dirty(self, reason=None):
        """Graph evidence changed; rebuild before treating the graph as current."""
        if self.graph_status == "active":
            self.graph_status = "needs_regeneration"
        if reason:
            logger.info("graph_marked_dirty reason=%s graph_id=%s", reason, self.graph_id)
        
    def add_to_graph(
        self, raw_data, doc_name, week_key, max_nodes_per_doc=None, pages=None
    ):
        if not raw_data:
            return
        mutated = False
        extracted_name = raw_data.get("graph_name", "")
        if (
            not self.graph_name
            and isinstance(extracted_name, str)
            and extracted_name.strip()
        ):
            self.graph_name = extracted_name.strip()
        for concept in raw_data.get("concepts", []):
            if not isinstance(concept, dict):
                continue
            name = str(concept.get("name", "")).lower().strip()
            description = str(concept.get("description", "") or "").strip()
            if not name:
                continue
            if max_nodes_per_doc is not None:
                doc_names = self._doc_concept_names.setdefault(doc_name, set())
                if name not in doc_names and len(doc_names) >= max_nodes_per_doc:
                    continue
                doc_names.add(name)
            # We allow duplicate concept names, as they might come from different weeks/docs
            self.concepts.append(
                {
                    "name": name,
                    "source": doc_name,
                    "week": week_key,
                    "pages": pages or [],
                    "description": description,
                }
            )

        for r in raw_data.get("relationships", []):
            from_node = r.get("from", "").lower().strip()
            to_node = r.get("to", "").lower().strip()
            relation = r.get("relation", "").lower().strip()
            summary = (r.get("summary") or "").strip()
            chunk_id = (r.get("chunk_id") or "").strip()
            if from_node and to_node:
                self.edges.append(
                    {
                        "from": from_node,
                        "to": to_node,
                        "relation": relation,
                        "source": doc_name,
                        "summary": summary,
                        "chunk_id": chunk_id,
                    }
                )
                mutated = True

        if mutated:
            self._mark_graph_dirty("graph evidence changed")

    def deduplicate_extracted_edges(self):
        """Remove exact duplicate extracted edges without making an LLM call."""
        unique = {}
        for edge in self.edges:
            key = (
                edge.get("from", "").strip().lower(),
                edge.get("to", "").strip().lower(),
                edge.get("relation", "").strip().lower(),
                edge.get("source", ""),
            )
            if key not in unique:
                unique[key] = {**edge, "chunk_ids": set()}
            chunk_ids = unique[key]["chunk_ids"]
            chunk_ids.update(edge.get("chunk_ids", []))
            if edge.get("chunk_id"):
                chunk_ids.add(edge["chunk_id"])
            if edge.get("summary") and not unique[key].get("summary"):
                unique[key]["summary"] = edge["summary"]
        for edge in unique.values():
            edge["chunk_ids"] = sorted(edge["chunk_ids"])
            edge.pop("chunk_id", None)
        removed = len(self.edges) - len(unique)
        self.edges = list(unique.values())
        if removed:
            logger.info(
                "extraction_edge_deduplication removed=%d remaining=%d",
                removed,
                len(self.edges),
            )
        return removed

    def prune_low_value_concepts(self, doc_name, week_key, min_mentions=2):
        """
        Drop concepts admitted from this doc/week that are mentioned fewer
        than min_mentions times across this doc's chunks. Off by default
        (call with min_mentions=1, or don't call it at all) so it never
        changes behavior unless explicitly requested.

        Before deleting them from the active graph, we stash the exact concept
        metadata and removed edges in a dormant cache so a later document can
        re-activate the concept if the embedding similarity is strong enough.
        """
        doc_week_concepts = [
            c
            for c in self.concepts
            if c["source"] == doc_name and c.get("week") == week_key
        ]
        if not doc_week_concepts:
            return

        counts = Counter(c["name"] for c in doc_week_concepts)

        dropped = {name for name, count in counts.items() if count < min_mentions}
        if not dropped:
            return

        dropped_edges = [
            e
            for e in self.edges
            if e.get("source") == doc_name
            and (e.get("from") in dropped or e.get("to") in dropped)
        ]

        for concept in doc_week_concepts:
            if concept["name"] not in dropped:
                continue
            cache_key = (concept["name"], doc_name, week_key)
            embedding = encode_texts([concept["name"]])[0]
            self.pruned_concept_cache[cache_key] = {
                "key": cache_key,
                "name": concept["name"],
                "source": doc_name,
                "week": week_key,
                "pages": list(concept.get("pages") or []),
                "embedding": np.asarray(embedding, dtype=float).tolist(),
                "edges": [
                    {**edge}
                    for edge in dropped_edges
                    if edge.get("from") == concept["name"]
                    or edge.get("to") == concept["name"]
                ],
            }

        self.concepts = [
            c
            for c in self.concepts
            if not (
                c["source"] == doc_name
                and c.get("week") == week_key
                and c["name"] in dropped
            )
        ]
        self.edges = [
            e
            for e in self.edges
            if not (
                e["source"] == doc_name and (e["from"] in dropped or e["to"] in dropped)
            )
        ]
        if doc_name in self._doc_concept_names:
            self._doc_concept_names[doc_name] -= dropped

        if dropped:
            self._mark_graph_dirty("concepts pruned")

        logger.info(
            "concepts_pruned document=%s week=%s dropped=%d",
            doc_name,
            week_key,
            len(dropped),
        )

    def reactivate_similar_pruned_concepts(
        self, threshold=PRUNED_REACTIVATION_SIMILARITY
    ):
        """Restore dormant pruned concepts when a new active concept is
        semantically similar enough, without needing an LLM call.

        This is intentionally conservative: it only checks cached pruned
        concepts against the current active graph and exits immediately when
        there are no candidates or no sufficiently similar match.
        """
        if not self.pruned_concept_cache:
            return {"matched": 0, "restored": []}

        active_concepts = [concept for concept in self.concepts if concept.get("name")]
        if not active_concepts:
            return {"matched": 0, "restored": []}

        active_names = [concept["name"] for concept in active_concepts]
        active_name_set = set(active_names)
        active_embeddings = np.asarray(encode_texts(active_names))

        restored = []
        for cache_key, entry in list(self.pruned_concept_cache.items()):
            if cache_key in self._reactivated_pruned_keys:
                continue

            pruned_name = entry["name"]
            if pruned_name in active_name_set:
                self._reactivated_pruned_keys.add(cache_key)
                continue

            cached_embedding = np.asarray(entry["embedding"], dtype=float).reshape(
                1, -1
            )
            sims = cosine_similarity(cached_embedding, active_embeddings)[0]
            best_index = int(np.argmax(sims))
            best_similarity = float(sims[best_index])

            if best_similarity < threshold:
                continue

            matched_name = active_names[best_index]
            if matched_name == pruned_name:
                self._reactivated_pruned_keys.add(cache_key)
                continue

            concept_record = {
                "name": matched_name,
                "source": entry["source"],
                "week": entry["week"],
                "pages": list(entry.get("pages") or []),
                "reactivated": True,
            }
            self.concepts.append(concept_record)
            restored.append(concept_record)
            self._reactivated_pruned_keys.add(cache_key)

            for edge in entry.get("edges") or []:
                candidate = {**edge}
                if candidate.get("from") == pruned_name:
                    candidate["from"] = matched_name
                if candidate.get("to") == pruned_name:
                    candidate["to"] = matched_name
                if not candidate.get("from") or not candidate.get("to"):
                    continue
                if candidate["from"] == candidate["to"]:
                    continue
                candidate["reactivated"] = True
                duplicate = any(
                    existing.get("from") == candidate.get("from")
                    and existing.get("to") == candidate.get("to")
                    and existing.get("relation") == candidate.get("relation")
                    and existing.get("source") == candidate.get("source")
                    for existing in self.edges
                )
                if not duplicate:
                    self.edges.append(candidate)

        if restored:
            logger.info(
                "pruned_concepts_reactivated count=%d threshold=%.2f",
                len(restored),
                threshold,
            )

        return {"matched": len(restored), "restored": restored}

    def fuse_graph(self):
        """Fuse extracted concepts and edges; replaces canonical_nodes (keeping restored aliases)
        and fused_edges."""
        logger.info(
            "graph_fusion_started concepts=%d edges=%d",
            len(self.concepts),
            len(self.edges),
        )
        self.reactivate_similar_pruned_concepts()

        self.remove_document_title_nodes()
        self.filter_dynamic_hubs(max_degree_ratio=0.15, absolute_max_degree=10)

        logger.info(
            "graph_fusion_using_active_graph concepts=%d edges=%d",
            len(self.concepts),
            len(self.edges),
        )
        restored_aliases = dict(self.canonical_nodes)
        self.canonical_nodes, self.fused_edges = graph_fusion.merge_graph(
            self, self.client
        )
        # Aliases restored from the previous version that are not names in this run keep
        # pointing at their concept, now under its current canonical name.
        present = {graph_fusion.normalize_concept(n) for n in
                   [c["name"] for c in self.concepts] + [e[k] for e in self.edges for k in ("from", "to")]}
        for alias, canonical in restored_aliases.items():
            if graph_fusion.normalize_concept(alias) not in present:
                self.canonical_nodes.setdefault(alias, self.canonical_nodes.get(canonical, canonical))

        self.graph_status = "active"

        logger.info(
            "graph_fusion_complete canonical_nodes=%d fused_edges=%d inferred_edges=%d",
            len(self.canonical_nodes),
            len(self.fused_edges),
            len(self.inferred_edges),
        )

    def _normalize_relation(self, relation):
        """
        Normalize relation labels to a fixed vocabulary.
        Handles common LLM output variance without an LLM call.
        """
        r = relation.lower().strip()

        synonyms = {
            "prerequisite": [
                "prerequisite",
                "prerequisite for",
                "prerequisite_for",
                "requires",
                "required for",
                "depends on",
            ],
            "causes": ["causes", "cause", "leads to", "results in", "produces"],
            "part_of": [
                "part of",
                "part_of",
                "component of",
                "composed of",
                "comprises",
            ],
            "related_to": [
                "related to",
                "related_to",
                "associated with",
                "connected to",
                "linked to",
                "relates to",
            ],
            "explains": ["explains", "explain", "defined as", "describes", "clarifies"],
            "enables": ["enables", "enable", "allows", "permits", "facilitates"],
        }

        for canonical, variants in synonyms.items():
            if r in variants:
                return canonical

        return r

    def _resolve_conflicts(self):
        """
        Conflict Resolution: When multiple edges exist between the same two nodes,
        normalize relation labels and select the first edge (conflicts are rare after normalization).
        No LLM call — all done via deterministic string normalization.
        """
        if not self.edges:
            return

        edge_groups = {}

        for edge in self.edges:
            from_node = self.canonical_nodes.get(edge["from"], edge["from"])
            to_node = self.canonical_nodes.get(edge["to"], edge["to"])

            normalized_relation = self._normalize_relation(edge["relation"])

            key = (from_node, to_node)
            if key not in edge_groups:
                edge_groups[key] = []

            edge_groups[key].append(
                {
                    "from": from_node,
                    "to": to_node,
                    "relation": normalized_relation,
                    "source": edge.get("source", "unknown"),
                    "original_relation": edge["relation"],
                }
            )

        for (from_node, to_node), edges in edge_groups.items():
            if len(edges) == 1:
                self.fused_edges.append(
                    {
                        "from": edges[0]["from"],
                        "to": edges[0]["to"],
                        "relation": edges[0]["relation"],
                        "source": edges[0]["source"],
                        "conflict_resolved": False,
                    }
                )
            else:
                relations = [e["relation"] for e in edges]

                if len(set(relations)) == 1:
                    self.fused_edges.append(
                        {
                            "from": from_node,
                            "to": to_node,
                            "relation": relations[0],
                            "source": edges[0]["source"],
                            "conflict_resolved": True,
                            "original_count": len(edges),
                        }
                    )
                else:
                    self.fused_edges.append(
                        {
                            "from": from_node,
                            "to": to_node,
                            "relation": relations[0],
                            "source": edges[0]["source"],
                            "conflict_resolved": True,
                            "original_count": len(edges),
                            "conflicting_relations": relations,
                        }
                    )

        logger.info(
            "conflict_resolution_complete edge_groups=%d fused_edges=%d",
            len(edge_groups),
            len(self.fused_edges),
        )

    def find_best_node(self, query):
        if not self.concepts:
            return None
        node_names = [c["name"] for c in self.concepts]
        node_embeddings = np.asarray(encode_texts(node_names))
        query_embedding = np.asarray(encode_texts([query.lower()]))
        similarities = cosine_similarity(query_embedding, node_embeddings)[0]
        best_idx = np.argmax(similarities)
        if similarities[best_idx] > 0.7:
            return node_names[best_idx]
        return None

    def filter_dynamic_hubs(self, max_degree_ratio=0.15, absolute_max_degree=10):
        """
        Dynamically prunes super-hub nodes based on graph connectivity metrics,
        regardless of the subject domain.
        """
        from collections import Counter

        if not self.concepts or not self.edges:
            return

        # Count degree per concept
        degrees = Counter()
        for e in self.edges:
            degrees[e["from"]] += 1
            degrees[e["to"]] += 1

        total_unique_concepts = len({c["name"] for c in self.concepts})

        # Determine degree threshold dynamically based on document size
        # A node connecting to >15% of all concepts in the document is an umbrella term
        dynamic_threshold = max(
            absolute_max_degree, int(total_unique_concepts * max_degree_ratio)
        )

        hub_nodes = {
            node for node, count in degrees.items() if count >= dynamic_threshold
        }

        if hub_nodes:
            logger.info(
                "dynamic_hub_pruning removed_hubs=%s threshold=%d total_concepts=%d",
                hub_nodes,
                dynamic_threshold,
                total_unique_concepts,
            )
            self.concepts = [c for c in self.concepts if c["name"] not in hub_nodes]
            self.edges = [
                e
                for e in self.edges
                if e["from"] not in hub_nodes and e["to"] not in hub_nodes
            ]

    def remove_document_title_nodes(self):
        """Remove concept nodes that simply repeat the overarching graph/document name."""
        if not self.graph_name:
            return

        title_words = set(self.graph_name.lower().split())

        # Filter out nodes that closely match the overall document title
        self.concepts = [
            c for c in self.concepts if c["name"].lower() != self.graph_name.lower()
        ]
        self.edges = [
            e
            for e in self.edges
            if e["from"].lower() != self.graph_name.lower()
            and e["to"].lower() != self.graph_name.lower()
        ]

    def get_relevant_subgraph(self, query, top_k_nodes=6, similarity_threshold=0.35):
        if not self.concepts:
            return {"nodes": [], "edges": [], "prompt_text": ""}

        node_names = [c["name"] for c in self.concepts]
        node_embeddings = np.asarray(encode_texts(node_names))
        query_embedding = np.asarray(encode_texts([query.lower()]))

        similarities = cosine_similarity(query_embedding, node_embeddings)[0]

        sorted_idx = np.argsort(similarities)[::-1]
        seed_names = set()
        for idx in sorted_idx[:top_k_nodes]:
            if similarities[idx] >= similarity_threshold:
                seed_names.add(node_names[idx])

        if not seed_names:
            seed_names.add(node_names[sorted_idx[0]])

        relevant_edges = [
            e for e in self.edges if e["from"] in seed_names or e["to"] in seed_names
        ]

        touched_names = seed_names.copy()
        for e in relevant_edges:
            touched_names.add(e["from"])
            touched_names.add(e["to"])

        name_to_meta = {c["name"]: c for c in self.concepts}
        relevant_nodes = []
        for name in touched_names:
            meta = name_to_meta.get(name, {"name": name, "source": "unknown"})
            relevant_nodes.append(
                {
                    "name": meta["name"],
                    "source": meta["source"],
                    "is_seed": name in seed_names,
                }
            )

        lines = ["Relevant knowledge-graph excerpt:"]
        seed_list = ", ".join(sorted(seed_names))
        lines.append(f"Key concepts: {seed_list}")
        if relevant_edges:
            lines.append("Connections:")
            for e in relevant_edges:
                lines.append(f"  {e['from']} --[{e['relation']}]--> {e['to']}")
        prompt_text = "\n".join(lines)

        return {
            "nodes": relevant_nodes,
            "edges": relevant_edges,
            "prompt_text": prompt_text,
        }

    def multi_hop_search(self, query, depth=2):
        """
        Multi-hop graph search: Find related concepts via graph traversal.
        """
        if not self.concepts:
            return {"nodes": [], "edges": []}

        working_edges = self.fused_edges if self.fused_edges else self.edges

        node_names = [c["name"] for c in self.concepts]
        node_embeddings = np.asarray(encode_texts(node_names))
        query_embedding = np.asarray(encode_texts([query.lower()]))
        similarities = cosine_similarity(query_embedding, node_embeddings)[0]

        seed_nodes = set()
        for idx, sim in enumerate(similarities):
            if sim > 0.5:
                seed_nodes.add(node_names[idx])

        if not seed_nodes:
            seed_nodes.add(node_names[np.argmax(similarities)])

        visited = seed_nodes.copy()
        frontier = seed_nodes.copy()

        for hop in range(depth - 1):
            next_frontier = set()
            for node in frontier:
                for edge in working_edges:
                    if edge["from"] == node and edge["to"] not in visited:
                        next_frontier.add(edge["to"])
                    elif edge["to"] == node and edge["from"] not in visited:
                        next_frontier.add(edge["from"])
                for edge in self.inferred_edges:
                    if edge["from"] == node and edge["to"] not in visited:
                        next_frontier.add(edge["to"])
                    elif edge["to"] == node and edge["from"] not in visited:
                        next_frontier.add(edge["from"])

            visited.update(next_frontier)
            frontier = next_frontier
            if not frontier:
                break

        name_to_meta = {c["name"]: c for c in self.concepts}
        multi_hop_nodes = []
        for name in visited:
            meta = name_to_meta.get(name, {"name": name, "source": "unknown"})
            multi_hop_nodes.append(
                {
                    "name": meta["name"],
                    "source": meta["source"],
                    "is_seed": name in seed_nodes,
                }
            )

        multi_hop_edges = [
            e for e in working_edges if e["from"] in visited and e["to"] in visited
        ]
        multi_hop_edges.extend(
            [
                e
                for e in self.inferred_edges
                if e["from"] in visited and e["to"] in visited
            ]
        )

        return {
            "nodes": multi_hop_nodes,
            "edges": multi_hop_edges,
            "seed_count": len(seed_nodes),
            "total_count": len(visited),
        }

    def set_graph(self, graph_id):
        self.graph_id = graph_id

    def mark_graph_for_regeneration(self, reason=None):
        self.graph_status = "needs_regeneration"
        if reason:
            logger.info("graph_marked_for_regeneration reason=%s graph_id=%s", reason, self.graph_id)

    def clear_graph_support(self):
        """Clear graph-derived state so the graph can be rebuilt from the remaining evidence."""
        self.concepts = []
        self.edges = []
        self.canonical_nodes = {}
        self.fused_edges = []
        self.inferred_edges = []
        self.graph_status = "needs_regeneration"

    def index_chunks(self, chunks, week_key=None):
        # store into the vector database
        texts = [chunk["text"] for chunk in chunks]

        embeddings = np.asarray(encode_texts(texts))

        ids = [
            chunk.get("chunk_id", f"chunk_{self._chroma_chunk_counter + i}")
            for i, chunk in enumerate(chunks)
        ]
        self._chroma_chunk_counter += len(texts)

        metadatas = []
        for chunk in chunks:
            meta = {
                "chunk_id": chunk.get("chunk_id", ids[len(metadatas)]),
                "pages": chunk["pages"],
                "source": chunk.get("source", "unknown"),
            }
            if self.graph_id is not None:
                meta["graph_id"] = self.graph_id
            if week_key is not None:
                meta["week"] = week_key
            metadatas.append(meta)

        self.collections.add(
            ids=ids, embeddings=embeddings, documents=texts, metadatas=metadatas
        )

    def retrieve_chunks(self, query, top_k=5, graph_id=None):
        target_graph_id = self.graph_id if graph_id is None else graph_id
        query_embedding = np.asarray(encode_texts([query.lower()]))

        query_kwargs = {
            "query_embeddings": query_embedding,
            "n_results": top_k,
        }
        if target_graph_id is not None:
            query_kwargs["where"] = {"graph_id": target_graph_id}

        results = self.collections.query(**query_kwargs)

        if not results["documents"] or not results["documents"][0]:
            return []  # case of failed chunk retrieval

        return [
            {
                "text": doc,
                "pages": meta["pages"],
                "source": meta.get("source", "unknown"),
                "similarity": 1 - dist,
            }
            for doc, meta, dist in zip(
                results["documents"][0],
                results["metadatas"][0],
                results["distances"][0],
            )
        ]

    # --- Evolution-only chunk store ---

    def index_chunks_for_week(self, chunks, week_key, doc_name):
        """Persist embedded chunks under `week_key`, additively (never wiped
        on later uploads), so concept-evolution summaries can retrieve
        grounded source text for any past week."""
        texts, pages_list = [], []
        for chunk in chunks:
            texts.append(chunk["text"])
            pages_list.append(chunk.get("pages", []))
        if not texts:
            return

        embeddings = np.asarray(encode_texts(texts))
        for chunk, text, pages, embedding in zip(chunks, texts, pages_list, embeddings):
            self.chunks_by_week[week_key].append(
                {
                    "chunk_id": chunk.get("chunk_id"),
                    "text": text,
                    "pages": pages,
                    "doc_name": doc_name,
                    "embedding": embedding,
                }
            )

    def retrieve_chunks_for_week(self, query, week_key, top_k=2):
        pool = self.chunks_by_week.get(week_key, [])
        if not pool:
            return []
        query_embedding = np.asarray(encode_texts([query.lower()]))
        chunk_embeddings = np.array([c["embedding"] for c in pool])
        similarities = cosine_similarity(query_embedding, chunk_embeddings)[0]
        top_indices = np.argsort(similarities)[-top_k:][::-1]

        return [
            {
                "text": pool[i]["text"],
                "pages": pool[i]["pages"],
                "doc_name": pool[i]["doc_name"],
                "similarity": round(float(similarities[i]), 3),
            }
            for i in top_indices
            if similarities[i] > 0.15
        ]

    @staticmethod
    def format_citation(pages):
        if not pages:
            return ""
        if len(pages) == 1:
            return f"p. {pages[0]}"
        if pages == list(range(pages[0], pages[0] + len(pages))):
            return f"pp. {pages[0]}-{pages[-1]}"
        return "pp. " + ", ".join(str(p) for p in pages)

    def rag_query(self, query, context=None):
        """
        GraphRAG Query: Combine the retrieved text chunks with
        the graph structure to improve the quality of responses.

        For broad queries with weak text matches, we lean on the
        graph to provide the user with an output.

        returns: dict with answer, citations, sources, subgraph and metadata
        """
        retrieved = self.retrieve_chunks(query, top_k=5)

        working_edges = self.fused_edges if self.fused_edges else self.edges
        
        graph_section=""
        concepts_mentioned=[]

        if working_edges:
            multi_hop_result = self.multi_hop_search(query, depth=2)
            subgraph = {
                "nodes": multi_hop_result.get("nodes", []),
                "edges": multi_hop_result.get("edges", []),
                "is_fused": bool(self.fused_edges),
            }

            if subgraph.get("edges"):
                graph_lines = ["Related concepts and connections:"]
                for e in subgraph.get("edges", [])[:8]:
                    graph_lines.append(f"  • {e['from']} --[{e['relation']}]--> {e['to']}")
                graph_section = "\n".join(graph_lines)
            else:
                graph_section = ""
        else:
            subgraph = {"nodes": [], "edges": [], "is_fused": False}

        if not retrieved:
            return {
                "answer": "No relevant text chunks found in your documents. Try a different query.",
                "citations": [],
                "citation_label": "",
                "chunks_used": 0,
                "chunks": [],
                "subgraph": {"nodes": [], "edges": []},
                "graph_search_depth": 0,
            }

        context_blocks, all_cited_pages, sources = [], [], []
        
        for i, r in enumerate(retrieved, start=1):
            citation = self.format_citation(r["pages"])
            header = f"[Chunk {i}: {citation}]" if citation else f"[Chunk {i}]"
            chunk_text = r["text"]
            context_blocks.append(f"{header}\n{chunk_text}")
            all_cited_pages.extend(r["pages"])
            
            sources.append(
                {
                    "fileName": r.get("source") or "document.pdf",
                    "page_number": r["pages"][0] if r.get("pages") else 1,
                    "snippet": (
                        (r["text"][:120] + "...") if len(r["text"]) > 120 else r["text"]
                    ),
                }
            )

        context_text = "\n\n".join(context_blocks)
        unique_pages = sorted(set(all_cited_pages))
        
        use_graph_primarily=prioritize_graph(retrieved)

        system_prompt = build_rag_system_prompt(prioritize_graph=use_graph_primarily)

        user_prompt = build_rag_user_prompt(context_text, query, graph_section)

        try:
            res = call_rag_llm(
                self.client,
                self.rag_model, 
                system_prompt,
                user_prompt
            )
            
            answer=res["answer"]
            used_sources=filter_sources_by_chunks_used(sources, set(res["chunks_used"]))
            concepts_mentioned = res["concepts_mentioned"]
            
        except (AttributeError, KeyError, ValueError) as e:
            logger.exception("rag_query_response_parse_failed error=%s", e)
            answer = "Error: Could not parse response from language model. Please try again"
            used_sources=[]
            concepts_mentioned = []
            
        except Exception as e:
            logger.exception("rag_query_llm_failed error_type=%s", type(e).__name__)
            answer =  f"Error querying language model: {type(e).__name__}"
            used_sources=[]
            concepts_mentioned = []

        return {
            "answer": answer,
            "citations": unique_pages,
            "citation_label": self.format_citation(unique_pages),
            "sources": used_sources,
            "chunks_used": len(used_sources),
            "chunks": [
                {
                    "text": (r["text"]),
                    "pages": r["pages"],
                    "citation": self.format_citation(r["pages"]),
                    "similarity": r["similarity"],
                }
                for r in retrieved
            ],
            "subgraph": subgraph,
            "graph_search_depth": 2 if working_edges else 0,
            "fusion_status": "fused" if self.fused_edges else "raw",
            "retrieval_method" : "text+graph" if graph_section else "text",
            "graph_used": bool(graph_section),
            "concepts_mentioned": concepts_mentioned
        }
