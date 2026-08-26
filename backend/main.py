import sys
from pathlib import Path
from collections import defaultdict, Counter

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from dotenv import load_dotenv
import os
import json
from services.document_service import extract_text
from groq import Groq, RateLimitError
from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_similarity
import numpy as np
from pathlib import Path
from datetime import datetime
from collections import defaultdict, Counter
import time
import asyncio
from chromadb import Client
from schemas import *
from cerebras.cloud.sdk import Cerebras, RateLimitError as RLE
from services.quiz_service import * 
from models.quiz_models import *
DEFAULT_QUIZ_QUESTION_COUNT = 5

backend_dir = Path(__file__).resolve().parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))
import evolution_data
import graph_fusion

load_dotenv()

chroma = Client()

if not os.getenv("GROQ_API_KEY"):
    raise RuntimeError("GROQ_API_KEY is not configured!")

# globals
OUTPUT_DIR = Path(__file__).parent / "outputs"
OUTPUT_DIR.mkdir(exist_ok=True)
UPLOAD_DIR = OUTPUT_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

UPLOADS_DIR = Path(__file__).parent / "uploads"
UPLOADS_DIR.mkdir(exist_ok=True)


app = FastAPI(
    title="StudyGraph API",
    description="FastAPI backend for StudyGraph LLM integration",
    version="0.1.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://localhost:\d+",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

client = Groq(api_key=os.getenv("GROQ_API_KEY"))

ce_client = Cerebras(api_key=os.getenv("CEREBRAS_API_KEY"))

engine = None

EXTRACTION_SEMAPHORE = asyncio.Semaphore(2)
EXTRACTION_MAX_RETRIES = 40  # matches evolution_data.py's MAX_RETRIES

SEED_ENTITY_PRIORITY_COUNT = 10   # how many of this doc's own seeds go in the prompt's priority line
CROSS_WEEK_CONCEPT_COUNT = 10     # how many known concepts from earlier weeks go in the prompt's known-concept line
DEFAULT_QUIZ_QUESTION_COUNT = 10


def save_seed_entities(seed_entities):
    """Save seed entities to graph_entities.json"""
    try:
        output_file = OUTPUT_DIR / "graph_entities.json"
        data = {
            "timestamp": datetime.now().isoformat(),
            "count": len(seed_entities),
            "seed_entities": seed_entities
        }
        with open(output_file, "w") as f:
            json.dump(data, f, indent=2)
        print(f"Saved {len(seed_entities)} seed entities to {output_file}")
        return str(output_file)
    except Exception as e:
        print(f"Error saving seed entities: {e}")
        return None

def save_extraction_result(timings, nodes, edges):
    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_file = OUTPUT_DIR / f"result_{timestamp}.json"

        data = {
            "timestamp": timestamp,
            "nodes": nodes,
            "edges": edges,
            "stats": {
                "total_nodes": len(nodes),
                "total_edges": len(edges),
                "timings": timings
            }
        }

        with open(output_file, 'w') as f:
            json.dump(data, f, indent=2)
        return str(output_file)
    except Exception as e:
        print(f"Error saving file: {e}")
        return None

def save_graph_json(nodes, edges, fusion_stats=None):
    """Save complete graph JSON to graph_json.json"""
    try:
        output_file = OUTPUT_DIR / "graph_json.json"
        data = {
            "timestamp": datetime.now().isoformat(),
            "nodes": nodes,
            "edges": edges,
            "stats": {
                "total_nodes": len(nodes),
                "total_edges": len(edges),
                "fusion_stats": fusion_stats or {}
            }
        }
        with open(output_file, "w") as f:
            json.dump(data, f, indent=2)
        print(f"Saved graph with {len(nodes)} nodes and {len(edges)} edges to {output_file}")
        return str(output_file)
    except Exception as e:
        print(f"Error saving graph JSON: {e}")
        return None

def get_collection(project_id, user_id="default"):
    return chroma.get_or_create_collection(
        name=f"user_{user_id}_project_{project_id}"
    )

def call_groq(prompt, text_batch):
    try:
        return client.chat.completions.create(
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": text_batch},
            ],
            model="openai/gpt-oss-20b",
            response_format={"type": "json_object"},
        )
    except Exception as e:
        # If strict JSON mode failed on validation, fall back without response_format constraints
        if "json_validate_failed" in str(e):
            print("Groq strict JSON validation failed. Retrying with explicit prompt...")
            return client.chat.completions.create(
                messages=[
                    {"role": "system", "content": prompt + "\nRespond in pure raw JSON only."},
                    {"role": "user", "content": text_batch},
                ],
                model="openai/gpt-oss-20b",
            )
        raise e
    
def call_cerebras(prompt, text_batch):
    return ce_client.chat.completions.create(
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": text_batch},
        ],
        model="gemma-4-31b",
        max_completion_tokens=2000,
    )

def call_llm(prompt, text_batch):
    return call_groq(prompt, text_batch)

class StudyGraphEngine:
    def __init__(self, collections):
        self.concepts = []
        self.edges = []
        self.model = SentenceTransformer('all-MiniLM-L6-v2')
        self.chunk_store = []
        self.graph = None
        self.seed_entities = []         # Top 20 global concepts identified before processing
        self.canonical_nodes = {}       # Map from variants to canonical names (post-fusion)
        self.fused_edges = []           # Edges after conflict resolution (post-fusion)
        self.inferred_edges = []        # Transitive edges inferred during fusion
        self.seed_generation_time = None        # float, seconds
        self.upload_latency = None              # float, total time for entire upload endpoint
        self.collections = collections
        self._doc_concept_names = {}    # doc_name -> set of admitted concept names (for max_nodes_per_doc)
        self._chroma_chunk_counter = 0  # monotonic counter so chunk ids never collide across uploads

        # --- Concept-evolution state ---
        self.week_counter = 1                    # Tracks upload batches as "weeks"
        self.uploads_by_week = {}                 # week_key -> filename
        self.chunks_by_week = defaultdict(list)   # week_key -> [{text, pages, doc_name, embedding}] (in-memory, separate from ChromaDB)

        # Concept names admitted from completed uploads, across all weeks.
        # Populated after each upload (see _run_upload_pipeline) and consulted
        # by extract_knowledge for later uploads, so a concept established in
        # an earlier week can still be extracted from a later document even if
        # it isn't among that document's own top seed entities.
        self.known_concepts = set()
        self.known_concept_embeddings = None   # np.ndarray aligned with sorted(known_concepts), rebuilt on update
        self._known_concepts_list = []          # sorted(known_concepts), kept alongside known_concept_embeddings

        self.concept_week_chunks = {}                       # (name, week) -> retrieved chunk list
        self.concept_week_summaries = {}                     # (name, week) -> LLM summary text
        self.concept_consecutive_comparisons = {}             # canonical concept_name -> [{from_week, to_week, comparison}]
        self.concept_overall_comparison = {}                  # canonical concept_name -> comparison text (first week vs. latest week)

        self.pipeline_status = {"stage": "idle"}

        self.degraded_summary_keys = set()
        self.quiz_sessions = {}

    def generate_seeds(self, all_pages):
        """
        Global Seed Generation dispatcher: Routes to BERTopic or LLM method.
        Records timing and method used for benchmarking.
        """
        start = time.time()
        self._generate_seeds_llm(all_pages)
        self.seed_generation_time = round(time.time() - start, 3)

    def _generate_seeds_llm(self, all_pages):
        full_text = " ".join([p.get("text", "") for p in all_pages])
        if not full_text.strip():
            return

        # Explicitly instruct JSON-only and avoid asking for markdown blocks
        seed_prompt = (
            "You are a helpful assistant that outputs JSON. "
            "Analyze the academic material and identify the TOP 20 most fundamental concepts. "
            "Return a single JSON object with a key 'seed_concepts' containing an array of strings. "
            "Do NOT include markdown formatting or code blocks."
        )

        try:
            response = call_llm(seed_prompt, f"Material:\n{full_text[:5000]}")
            content = response.choices[0].message.content

            if not content or not content.strip():
                print("Empty response from LLM for seed generation")
                self.seed_entities = []
                return

            # Clean potential markdown wrappers if the model generated them
            content = content.strip()
            if "```" in content:
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
                content = content.strip()

            seed_json = json.loads(content)
            self.seed_entities = [s.lower().strip() for s in seed_json.get("seed_concepts", [])]
            print(f"LLM generated {len(self.seed_entities)} seed entities: {self.seed_entities}")
        except Exception as e:
            print(f"Error generating seeds (LLM): {e}")
            self.seed_entities = []

    def _deduplicate_seeds_semantically(self, seeds, threshold=0.85):
        """
        Remove semantically redundant seeds using cosine similarity.
        Prevents variants like 'deep fake', 'deep fakes', 'deepfake' all surviving.
        """
        if not seeds:
            return seeds
        embeddings = self.model.encode(seeds)
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
        print(f"Semantic deduplication: {len(seeds)} → {len(kept)} seeds")
        return kept

    def get_chunks(self, pages, source="unknown", size=2000, overlap=400, week_key=None):
        chunks = []
        full_text = ""
        position_to_page = []

        for p in pages:
            full_text += p["text"] + " "
            position_to_page.extend([p["page"]] * (len(p["text"]) + 1))

        if not full_text.strip():
            return []

        step = max(size - overlap, 1)
        start = 0
        while start < len(full_text):
            end = min(start + size, len(full_text))
            chunk_text = full_text[start:end]
            spanned_pages = sorted(set(position_to_page[start:end]))
            chunks.append({"text": chunk_text, "pages": spanned_pages, "source": source})
            start += step

        self.index_chunks(chunks, week_key=week_key)

        if week_key is not None:
            self.index_chunks_for_week(chunks, week_key, source)

        return chunks

    def rebuild_known_concept_embeddings(self):
        """Recompute the embedding index for known_concepts. Call after
        known_concepts changes, before relevant_known_concepts is used."""
        self._known_concepts_list = sorted(self.known_concepts)
        if self._known_concepts_list:
            self.known_concept_embeddings = self.model.encode(self._known_concepts_list)
        else:
            self.known_concept_embeddings = None

    def relevant_known_concepts(self, text, top_k=CROSS_WEEK_CONCEPT_COUNT):
        """Known concepts (from prior weeks) most relevant to `text`, ranked
        by embedding similarity rather than a flat alphabetical slice."""
        if not self._known_concepts_list or self.known_concept_embeddings is None:
            return []
        query_embedding = self.model.encode([text[:2000].lower()])
        sims = cosine_similarity(query_embedding, self.known_concept_embeddings)[0]
        top_idx = np.argsort(sims)[::-1][:top_k]
        return [self._known_concepts_list[i] for i in top_idx]

    async def extract_knowledge(self, text_batch, known_concepts=None):
        raw_data = None  # ← initialize here so except blocks can always reference it

        if self.seed_entities:
            seed_list = ", ".join(self.seed_entities[:SEED_ENTITY_PRIORITY_COUNT])
            priority_line = f"Extract academic concepts and relationships, prioritizing these key entities: {seed_list}.\n"
        else:
            priority_line = "Extract academic concepts and relationships from the text.\n"

        known_line = ""
        if known_concepts:
            known_list = ", ".join(known_concepts)
            known_line = (
                f"If any of these concepts are referenced in this text, even in passing, "
                f"extract them too using this exact name: {known_list}.\n"
            )

        prompt = f"""You are a JSON-only extraction engine. You must respond with ONLY a valid JSON object and absolutely nothing else — no explanation, no markdown, no code fences.

    {priority_line}{known_line}
    Relationship types: prerequisite, causes, part_of, explains, enables, related_to
    For each relationship, include both:
    - summary: a short explanation (one sentence max)
    - context: the exact phrase or sentence from the source text that supports the relationship

    Respond with ONLY this exact JSON structure:
    {{"concepts": ["concept1", "concept2"], "relationships": [{{"from": "concept1", "to": "concept2", "relation": "enables", "summary": "Short explanation of the supporting text segment.", "context": "Exact phrase or sentence from the source text."}}]}}"""

        try:
            async with EXTRACTION_SEMAPHORE:
                response = await asyncio.to_thread(call_llm, prompt, text_batch)
            content = response.choices[0].message.content

            if not content or not content.strip():
                print("LLM returned empty response")
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
            print("Done")
            return {
                "concepts": validated.concepts,
                "relationships": [
                    {
                        "from": r.from_,
                        "to": r.to,
                        "relation": r.relation,
                        "summary": getattr(r, "summary", "") or "",
                        "context": getattr(r, "context", "") or "",
                    }
                    for r in validated.relationships
                ]
            }
        except ValueError as e:
            print(f"Extraction validation error: {e}")
            print(f"Raw data was: {raw_data}")
            return {"concepts": [], "relationships": []}
        except json.JSONDecodeError as e:
            print(f"JSON parse error: {e}")
            return {"concepts": [], "relationships": []}
        except (RateLimitError, RLE) as e:
            print(e)
            return {"concepts": [], "relationships": []}
        except Exception as e:
            print(f"Error calling LLM: {e}")
            print(f"Raw data was: {raw_data}")
            return {"concepts": [], "relationships": []}

    def add_to_graph(self, raw_data, doc_name, week_key, max_nodes_per_doc=None, pages=None):
        if not raw_data:
            return
        for c in raw_data.get("concepts", []):
            if not isinstance(c, str):
                continue
            name = c.lower().strip()
            if not name:
                continue
            if max_nodes_per_doc is not None:
                doc_names = self._doc_concept_names.setdefault(doc_name, set())
                if name not in doc_names and len(doc_names) >= max_nodes_per_doc:
                    continue
                doc_names.add(name)
            # We allow duplicate concept names, as they might come from different weeks/docs
            self.concepts.append({"name": name, "source": doc_name, "week": week_key, "pages": pages or []})

        for r in raw_data.get("relationships", []):
            from_node = r.get("from", "").lower().strip()
            to_node   = r.get("to", "").lower().strip()
            relation  = r.get("relation", "").lower().strip()
            summary   = (r.get("summary") or "").strip()
            if from_node and to_node:
                context = (r.get("context") or "").strip()
                self.edges.append(
                    {
                        "from": from_node,
                        "to": to_node,
                        "relation": relation,
                        "source": doc_name,
                        "summary": summary,
                        "context": context,
                    }
                )

    def prune_low_value_concepts(self, doc_name, week_key, min_mentions=2):
        """
        Drop concepts admitted from this doc/week that show no sign of being
        a "key" concept: mentioned fewer than min_mentions times across this
        doc's chunks, and not one of the curated seed_entities. Off by
        default (call with min_mentions=1, or don't call it at all) so it
        never changes behavior unless explicitly requested.
        """
        doc_week_concepts = [c for c in self.concepts if c["source"] == doc_name and c.get("week") == week_key]
        if not doc_week_concepts:
            return

        counts = Counter(c["name"] for c in doc_week_concepts)
        seeds = set(self.seed_entities)

        dropped = {
            name for name, count in counts.items()
            if count < min_mentions and name not in seeds
        }
        if not dropped:
            return

        self.concepts = [
            c for c in self.concepts
            if not (c["source"] == doc_name and c.get("week") == week_key and c["name"] in dropped)
        ]
        self.edges = [
            e for e in self.edges
            if not (e["source"] == doc_name and (e["from"] in dropped or e["to"] in dropped))
        ]
        if doc_name in self._doc_concept_names:
            self._doc_concept_names[doc_name] -= dropped

        print(f"Pruned {len(dropped)} low-value concept(s) from {doc_name} ({week_key}): {sorted(dropped)}")

    def fuse_graph(self):
        """Fuse extracted concepts and edges; replaces canonical_nodes and fused_edges."""
        print("Starting Global Knowledge Fusion...")
        self.canonical_nodes, self.fused_edges = graph_fusion.merge_graph(self, client)

        print(f"Fusion complete. Canonical nodes: {len(self.canonical_nodes)}, "
              f"Fused edges: {len(self.fused_edges)}, Inferred edges: {len(self.inferred_edges)}")

    def _merge_entities(self):
        """
        Entity Merging: Use cosine similarity to identify and merge semantically identical nodes.
        No LLM call — deterministic, fast, reproducible.
        """
        if not self.concepts:
            return

        node_names = [c["name"] for c in self.concepts]
        if len(node_names) < 2:
            return

        embeddings = self.model.encode(node_names)

        merged = set()
        threshold = 0.80  # tune this based on false positives you see

        for i in range(len(node_names)):
            if node_names[i] in merged:
                continue

            sims = cosine_similarity([embeddings[i]], embeddings)[0]

            for j in range(i + 1, len(node_names)):
                if node_names[j] in merged:
                    continue

                if sims[j] >= threshold:
                    self.canonical_nodes[node_names[j]] = node_names[i]
                    merged.add(node_names[j])

        print(f"Entity merging: {len(self.canonical_nodes)} merges via cosine similarity (threshold={threshold})")

    def _normalize_relation(self, relation):
        """
        Normalize relation labels to a fixed vocabulary.
        Handles common LLM output variance without an LLM call.
        """
        r = relation.lower().strip()

        synonyms = {
            "prerequisite": ["prerequisite", "prerequisite for", "prerequisite_for", "requires", "required for", "depends on"],
            "causes": ["causes", "cause", "leads to", "results in", "produces"],
            "part_of": ["part of", "part_of", "component of", "composed of", "comprises"],
            "related_to": ["related to", "related_to", "associated with", "connected to", "linked to", "relates to"],
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

            edge_groups[key].append({
                "from": from_node,
                "to": to_node,
                "relation": normalized_relation,
                "source": edge.get("source", "unknown"),
                "original_relation": edge["relation"]
            })

        for (from_node, to_node), edges in edge_groups.items():
            if len(edges) == 1:
                self.fused_edges.append({
                    "from": edges[0]["from"],
                    "to": edges[0]["to"],
                    "relation": edges[0]["relation"],
                    "source": edges[0]["source"],
                    "conflict_resolved": False
                })
            else:
                relations = [e["relation"] for e in edges]

                if len(set(relations)) == 1:
                    self.fused_edges.append({
                        "from": from_node,
                        "to": to_node,
                        "relation": relations[0],
                        "source": edges[0]["source"],
                        "conflict_resolved": True,
                        "original_count": len(edges)
                    })
                else:
                    self.fused_edges.append({
                        "from": from_node,
                        "to": to_node,
                        "relation": relations[0],
                        "source": edges[0]["source"],
                        "conflict_resolved": True,
                        "original_count": len(edges),
                        "conflicting_relations": relations
                    })

        print(f"Conflict resolution: Processed {len(edge_groups)} edge groups, "
              f"created {len(self.fused_edges)} fused edges")

    def find_best_node(self, query):
        if not self.concepts:
            return None
        node_names = [c["name"] for c in self.concepts]
        node_embeddings = self.model.encode(node_names)
        query_embedding = self.model.encode([query.lower()])
        similarities = cosine_similarity(query_embedding, node_embeddings)[0]
        best_idx = np.argmax(similarities)
        if similarities[best_idx] > 0.7:
            return node_names[best_idx]
        return None

    def get_relevant_subgraph(self, query, top_k_nodes=6, similarity_threshold=0.35):
        if not self.concepts:
            return {"nodes": [], "edges": [], "prompt_text": ""}

        node_names      = [c["name"] for c in self.concepts]
        node_embeddings = self.model.encode(node_names)
        query_embedding = self.model.encode([query.lower()])

        similarities = cosine_similarity(query_embedding, node_embeddings)[0]

        sorted_idx = np.argsort(similarities)[::-1]
        seed_names = set()
        for idx in sorted_idx[:top_k_nodes]:
            if similarities[idx] >= similarity_threshold:
                seed_names.add(node_names[idx])

        if not seed_names:
            seed_names.add(node_names[sorted_idx[0]])

        relevant_edges = [
            e for e in self.edges
            if e["from"] in seed_names or e["to"] in seed_names
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
                {"name": meta["name"], "source": meta["source"], "is_seed": name in seed_names}
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
        node_embeddings = self.model.encode(node_names)
        query_embedding = self.model.encode([query.lower()])
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
            multi_hop_nodes.append({
                "name": meta["name"],
                "source": meta["source"],
                "is_seed": name in seed_nodes
            })

        multi_hop_edges = [
            e for e in working_edges
            if e["from"] in visited and e["to"] in visited
        ]
        multi_hop_edges.extend([
            e for e in self.inferred_edges
            if e["from"] in visited and e["to"] in visited
        ])

        return {
            "nodes": multi_hop_nodes,
            "edges": multi_hop_edges,
            "seed_count": len(seed_nodes),
            "total_count": len(visited)
        }

    def index_chunks(self, chunks, week_key=None):
        # store into the vector database
        texts = [chunk["text"] for chunk in chunks]

        embeddings = self.model.encode(texts)

        ids = [f"chunk_{self._chroma_chunk_counter + i}" for i in range(len(texts))]
        self._chroma_chunk_counter += len(texts)

        metadatas = []
        for chunk in chunks:
            meta = {"pages": chunk["pages"], "source": chunk.get("source", "unknown")}
            if week_key is not None:
                meta["week"] = week_key
            metadatas.append(meta)

        self.collections.add(
            ids=ids,
            embeddings=embeddings,
            documents=texts,
            metadatas=metadatas
        )

    def retrieve_chunks(self, query, top_k=3):
        query_embedding  = self.model.encode([query.lower()])

        results = self.collections.query(
            query_embeddings=query_embedding,
            n_results=top_k
        )

        if not results["documents"] or not results["documents"][0]:
            return []  # case of failed chunk retrieval

        return [
            {
                "text": doc,
                "pages": meta["pages"],
                "source": meta.get("source", "unknown"),
                "similarity": 1 - dist
            }
            for doc, meta, dist in zip(results["documents"][0], results["metadatas"][0], results["distances"][0])
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

        embeddings = self.model.encode(texts)
        for text, pages, embedding in zip(texts, pages_list, embeddings):
            self.chunks_by_week[week_key].append({
                "text": text, "pages": pages, "doc_name": doc_name, "embedding": embedding,
            })

    def retrieve_chunks_for_week(self, query, week_key, top_k=2):
        pool = self.chunks_by_week.get(week_key, [])
        if not pool:
            return []
        query_embedding = self.model.encode([query.lower()])
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
        RAG Query: Retrieves text chunks and optionally uses graph context.
        Returns concise, well-cited answers.
        """
        retrieved = self.retrieve_chunks(query, top_k=3)

        if not retrieved:
            return {
                "answer": "No relevant text chunks found in your documents. Try a different query.",
                "citations": [],
                "citation_label": "",
                "chunks_used": 0,
                "chunks": [],
                "subgraph": {"nodes": [], "edges": []},
                "graph_search_depth": 0
            }

        context_blocks, all_cited_pages, sources = [], [], []
        for r in retrieved:
            citation = self.format_citation(r["pages"])
            header = f"[{citation}]" if citation else "[unknown page]"
            chunk_text = r["text"][:400]
            context_blocks.append(f"{header}\n{chunk_text}")
            all_cited_pages.extend(r["pages"])
            sources.append({
                "fileName": r.get("source") or "document.pdf",
                "page_number": r["pages"][0] if r.get("pages") else 1,
                "snippet": (r["text"][:120] + "...") if len(r["text"]) > 120 else r["text"],
            })

        context_text = "\n\n".join(context_blocks)
        unique_pages = sorted(set(all_cited_pages))

        graph_section = ""
        working_edges = self.fused_edges if self.fused_edges else self.edges
        if working_edges:
            multi_hop_result = self.multi_hop_search(query, depth=2)
            subgraph = {
                "nodes": multi_hop_result.get("nodes", []),
                "edges": multi_hop_result.get("edges", []),
                "is_fused": bool(self.fused_edges)
            }

            if subgraph.get("edges"):
                graph_lines = ["Related concepts from the graph:"]
                for e in subgraph.get("edges", [])[:5]:
                    graph_lines.append(f"  {e['from']} [{e['relation']}] {e['to']}")
                graph_section = "\n".join(graph_lines)
        else:
            subgraph = {"nodes": [], "edges": [], "is_fused": False}

        system_prompt = """You are a concise study assistant. Answer questions directly and briefly.

    CITATION RULES:
    - ALWAYS cite page numbers for facts from the text
    - Format: [p. 5] for single page, [pp. 3-5] for ranges
    - Put citations immediately after each claim
    - Keep answers to 2-3 sentences maximum
    - If graph information is relevant, mention it but don't rely on it if text is available"""

        user_prompt = f"""TEXT FROM DOCUMENTS (cite these):
    {context_text}

    {f"Related graph connections:" + chr(10) + graph_section if graph_section else ""}

    QUESTION: {query}

    Answer directly and briefly. Always cite the page numbers."""

        try:
            response = client.chat.completions.create(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                model="openai/gpt-oss-20b",
            )
            answer = response.choices[0].message.content
        except Exception as e:
            print(f"Error in RAG query: {e}")
            answer = "Error processing query"

        return {
            "answer": answer,
            "citations": unique_pages,
            "citation_label": self.format_citation(unique_pages),
            "sources": sources,
            "chunks_used": len(retrieved),
            "chunks": [
                {
                    "text": r["text"][:200] + "..." if len(r["text"]) > 200 else r["text"],
                    "pages": r["pages"],
                    "citation": self.format_citation(r["pages"]),
                    "similarity": r["similarity"],
                }
                for r in retrieved
            ],
            "subgraph": subgraph,
            "graph_search_depth": 2 if working_edges else 0,
            "fusion_status": "fused" if self.fused_edges else "raw"
        }

async def extract_with_exp_backoff(text, known_concepts=None, retries=5):
    """
    Helper method to extract concepts and relationships with exponential
    backoff.
    """
    delay = 5

    for attempt in range(retries):
        try:
            return await (engine.extract_knowledge(text, known_concepts=known_concepts))
        except RateLimitError as e:
            await asyncio.sleep(delay)
            delay *= 2
    raise Exception("Maximum retries exceeded!")

# API Endpoints
@app.get("/health")
async def health_check():
    return {"status": "ok"}


async def _run_upload_pipeline(engine, pages, filename, week_key, upload_start, max_nodes, min_mentions):
    """
    Everything slow and LLM-dependent about an upload: seed generation (first
    upload only), chunk extraction, graph building, and — for every concept
    that survives pruning — its cheap-tier description (see
    evolution_data.precompute_week_summaries).

    The full evolution tier (every week's summary + every week-to-week
    comparison — see get_concept_detail) is deliberately NOT precomputed
    here — it's gated behind the explicit "View Full Evolution" action.
    """
    try:
        safe_name = Path(filename).name

        engine.generate_seeds(pages)
        save_seed_entities(engine.seed_entities)

        chunks = engine.get_chunks(pages, source=safe_name, week_key=week_key)

        # Snapshot known_concepts before this document contributes anything,
        # so extraction is only nudged toward concepts from earlier weeks.
        engine.rebuild_known_concept_embeddings()

        extraction_tasks = [
            extract_with_exp_backoff(
                chunk["text"],
                known_concepts=engine.relevant_known_concepts(chunk["text"]),
            )
            for chunk in chunks
        ]
        results = await asyncio.gather(*extraction_tasks)

        for i, data in enumerate(results):
            engine.add_to_graph(data, filename, week_key, max_nodes_per_doc=max_nodes, pages=chunks[i].get("pages"))
            print(f"Processed chunk {i + 1}/{len(results)}")

        if min_mentions > 1:
            engine.prune_low_value_concepts(filename, week_key, min_mentions=min_mentions)

        engine.known_concepts.update(
            c["name"] for c in engine.concepts
            if c["source"] == filename and c["week"] == week_key
        )

        doc_week_pairs = {
            (c["name"], c["week"]) for c in engine.concepts
            if c["source"] == filename and c["week"] == week_key
        }
        await evolution_data.precompute_week_summaries(engine, client, doc_week_pairs)

        working_edges = engine.fused_edges if engine.fused_edges else engine.edges
        graph_save_path = save_graph_json(
            [{"name": c["name"], "source": c["source"]} for c in engine.concepts],
            working_edges
        )

        upload_latency = round(time.time() - upload_start, 3)
        save_extraction_result(
            {
                "number of chunks": len(chunks),
                "latency": upload_latency,
                "time per chunk": upload_latency / len(chunks) if chunks else 0
            },
            engine.concepts,
            working_edges
        )
        engine.upload_latency = upload_latency

        engine.pipeline_status = {
            "stage": "done",
            "operation": "upload",
            "week": week_key,
            "filename": filename,
            "chunks": len(chunks),
            "seeds_generated": len(engine.seed_entities),
            "seed_entities": engine.seed_entities,
            "raw_concepts": len(engine.concepts),
            "total_edges": len(working_edges),
            "graph_saved_to": graph_save_path,
            "error": None,
        }
    except Exception as e:
        print(f"Background upload pipeline failed for {filename}: {e}")
        import traceback
        traceback.print_exc()
        engine.pipeline_status = {
            "stage": "error", "operation": "upload",
            "week": week_key, "filename": filename, "error": str(e),
        }


@app.post("/api/upload")
async def upload_pdf(file: UploadFile = File(...), max_nodes: int = None, min_mentions: int = 1):
    """
    Upload a PDF. Only the fast, local, deterministic steps (read the file,
    save it to disk, parse its text, assign a week) happen before this
    responds. Extraction and graph building happen afterward in a
    background task (see _run_upload_pipeline). Poll GET /api/upload/status
    for progress.

    max_nodes: optional hard cap on distinct concepts admitted from this
    document (default: no cap).

    min_mentions: "key concept" threshold for pruning low-value concepts
    after extraction. Default 1 == pruning disabled. Raise to 2+ to drop
    concepts extracted only once and not in seed_entities.
    """
    if not os.getenv("GROQ_API_KEY"):
        raise HTTPException(
            status_code=503,
            detail="The AI Service is temporarily down. Please try again later."
        )

    global engine
    upload_start = time.time()

    if engine is None:
        engine = StudyGraphEngine(get_collection(1))

    safe_filename = Path(file.filename).name

    try:
        file_bytes = await file.read()
        pages = extract_text(file_bytes, safe_filename)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    (UPLOADS_DIR / safe_filename).write_bytes(file_bytes)

    current_week_key = f"Week {engine.week_counter}"
    engine.week_counter += 1
    engine.uploads_by_week[current_week_key] = safe_filename

    engine.pipeline_status = {
        "stage": "running", "operation": "upload",
        "week": current_week_key, "filename": safe_filename, "error": None,
    }
    asyncio.create_task(
        _run_upload_pipeline(engine, pages, safe_filename, current_week_key, upload_start, max_nodes, min_mentions)
    )

    return {
        "status": "processing",
        "message": f"Received {len(pages)} pages — processing in the background.",
        "week": current_week_key,
        "filename": safe_filename,
    }


@app.get("/api/upload/status")
async def get_pipeline_status():
    """Poll target for the background upload pipeline (see StudyGraphEngine.pipeline_status)."""
    global engine
    if engine is None:
        return {"stage": "idle"}
    return engine.pipeline_status

@app.get("/api/files/{filename}")
async def serve_uploaded_file(filename: str):
    safe_name = Path(filename).name
    upload_path = UPLOAD_DIR / safe_name
    if not upload_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(
        upload_path,
        media_type="application/pdf",
        headers={"Content-Disposition": f"inline; filename=\"{safe_name}\""},
    )

@app.get("/api/documents/file/{filename}")
async def get_document_file(filename: str):
    """Serves a previously-uploaded PDF back (e.g. for the 'open source at page N' viewer)."""
    safe_name = Path(filename).name
    pdf_path = UPLOADS_DIR / safe_name
    if not pdf_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(pdf_path, media_type="application/pdf", filename=safe_name)


def _source_page_fields(pages):
    """
    pages: sorted list of distinct page ints a concept appeared on within one
    source file. Returns the jump target for the PDF viewer (first page) plus
    a compact human-readable label ("43", "43-48", "43, 47, 50").
    """
    if not pages:
        return {"page": None, "pageLabel": None}
    if len(pages) == 1:
        label = str(pages[0])
    elif pages == list(range(pages[0], pages[-1] + 1)):
        label = f"{pages[0]}-{pages[-1]}"
    else:
        label = ", ".join(str(p) for p in pages)
    return {"page": pages[0], "pageLabel": label}


@app.get("/api/graph")
async def get_graph():
    """Get current knowledge graph (uses fused graph if available), with
    week-based color coding for the evolution feature."""
    global engine
    if engine is None:
        return GraphResponse(nodes=[], edges=[])

    week_color_map = evolution_data.get_week_color_map(engine)

    working_edges = engine.fused_edges if engine.fused_edges else engine.edges

    concept_relations = {}
    for e in working_edges:
        from_node = e["from"]
        to_node = e["to"]
        if from_node not in concept_relations:
            concept_relations[from_node] = []
        if to_node not in concept_relations:
            concept_relations[to_node] = []
        concept_relations[from_node].append(f"{e['relation']} → {to_node}")
        concept_relations[to_node].append(f"{from_node} → {e['relation']}")

    aggregated_concepts = {}
    for c in engine.concepts:
        canonical_name = engine.canonical_nodes.get(c["name"], c["name"])
        if canonical_name not in aggregated_concepts:
            aggregated_concepts[canonical_name] = {"sources": defaultdict(set), "weeks": set()}
        aggregated_concepts[canonical_name]["sources"][c["source"]].update(
            p for p in (c.get("pages") or []) if isinstance(p, int)
        )
        week = c.get("week")
        if week:
            aggregated_concepts[canonical_name]["weeks"].add(week)

    nodes = []
    for name, data in aggregated_concepts.items():
        appears_in_weeks = len(data["weeks"])
        node_color = evolution_data.DEFAULT_COLOR

        if appears_in_weeks > 1:
            node_color = evolution_data.MULTI_WEEK_COLOR
        elif appears_in_weeks == 1:
            first_week = list(data["weeks"])[0]
            node_color = week_color_map.get(first_week, evolution_data.DEFAULT_COLOR)

        if not any(n["name"] == name for n in nodes):
            nodes.append({
                "id": name.replace(" ", "_"),
                "name": name,
                "source": [
                    {"file": f, **_source_page_fields(sorted(page_set))}
                    for f, page_set in data["sources"].items()
                ],
                "description": "; ".join(concept_relations.get(name, []))[:150] or name,
                "is_canonical": name in engine.canonical_nodes.values(),
                "color": node_color,
                "appears_in_weeks": appears_in_weeks,
            })

    valid_node_ids = {n["id"] for n in nodes}

    edges = [
        {
            "id": f"{e['from']}_to_{e['to']}",
            "source": e["from"].replace(" ", "_"),
            "target": e["to"].replace(" ", "_"),
            "label": e["relation"],
            "relation": e["relation"],
            "summary": e.get("summary", ""),
            "context": e.get("context", ""),
            "conflict_resolved": e.get("conflict_resolved", False)
        }
        for e in working_edges
        if e["from"].replace(" ", "_") in valid_node_ids and e["to"].replace(" ", "_") in valid_node_ids
    ]

    if engine.inferred_edges:
        for e in engine.inferred_edges:
            if (e["from"].replace(" ", "_") in valid_node_ids and
                    e["to"].replace(" ", "_") in valid_node_ids):
                edges.append({
                    "id": f"{e['from']}_to_{e['to']}_inferred",
                    "source": e["from"].replace(" ", "_"),
                    "target": e["to"].replace(" ", "_"),
                    "label": e["relation"],
                    "relation": e["relation"],
                    "summary": e.get("summary", ""),
                    "context": e.get("context", ""),
                    "conflict_resolved": False,
                    "is_inferred": True
                })

    graph_save_path = save_graph_json(nodes, edges, {
        "canonical_nodes": len(engine.canonical_nodes),
        "fused_edges": len(engine.fused_edges),
        "inferred_edges": len(engine.inferred_edges)
    })

    legend = {
        "weeks": [{"week": week, "color": color} for week, color in week_color_map.items()],
        "multi_week": {"label": "Spans multiple weeks", "color": evolution_data.MULTI_WEEK_COLOR},
        "no_week_data": {"label": "No week data", "color": evolution_data.DEFAULT_COLOR},
    }

    return {
        "nodes": nodes,
        "edges": edges,
        "is_fused": bool(engine.fused_edges),
        "graph_saved_to": graph_save_path,
        "legend": legend,
        "stats": {
            "canonical_nodes": len(engine.canonical_nodes),
            "fused_edges": len(engine.fused_edges),
            "inferred_edges": len(engine.inferred_edges)
        }
    }


@app.post("/api/query")
async def query_rag(request: QueryRequest):
    """Query the knowledge graph with RAG"""
    if not os.getenv("GROQ_API_KEY"):
        raise HTTPException(
            status_code=503,
            detail="The AI Service is temporarily down. Please try again later."
        )
    global engine
    if engine is None:
        raise HTTPException(status_code=400, detail="No documents uploaded yet")

    try:
        result = engine.rag_query(request.query)
        return result
    except HTTPException as e:
        if e.status_code in [401, 402, 403, 429]:
            raise HTTPException(
                status_code=503,
                detail="The service is temporarily down. Please try again soon."
            )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=str(e)
        )

@app.post("/api/fuse")
async def fuse_knowledge_graph():
    """
    Trigger the Global Knowledge Fusion pipeline (Phase 3 of BATCH-STREAM-BATCH).
    Call this endpoint after all documents are uploaded.
    """
    global engine
    if engine is None:
        raise HTTPException(status_code=400, detail="No documents uploaded yet")

    try:
        engine.fuse_graph()

        working_edges = engine.fused_edges if engine.fused_edges else engine.edges
        fused_nodes = [{"name": c["name"], "source": c["source"]} for c in engine.concepts]
        graph_save_path = save_graph_json(fused_nodes, working_edges, {
            "canonical_nodes": len(engine.canonical_nodes),
            "fused_edges": len(engine.fused_edges),
            "inferred_edges": len(engine.inferred_edges)
        })

        return {
            "status": "success",
            "message": "Knowledge graph fusion complete",
            "pipeline_phase": "batch_fusion",
            "canonical_nodes_created": len(engine.canonical_nodes),
            "fused_edges": len(engine.fused_edges),
            "inferred_edges": len(engine.inferred_edges),
            "total_concepts": len(engine.concepts),
            "total_edges": len(engine.edges),
            "graph_saved_to": graph_save_path
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# --- Concept-evolution endpoints ---

@app.get("/api/evolution/graph")
async def get_evolution_graph_data(week: str = None):
    """Get the main evolution graph data, optionally filtered by week."""
    global engine
    if engine is None:
        raise HTTPException(status_code=400, detail="No documents uploaded yet")
    return evolution_data.get_graph_data(engine, week)


@app.get("/api/evolution/concept/{concept_name}/description")
async def get_evolution_concept_description(concept_name: str):
    """Cheap tier: a single grounded summary from the concept's
    earliest-appearance week only. Meant to back a plain node click."""
    global engine
    if engine is None:
        raise HTTPException(status_code=400, detail="No documents uploaded yet")
    description = await evolution_data.get_concept_description(engine, concept_name, client)
    if not description:
        raise HTTPException(status_code=404, detail="Concept not found")
    return description


@app.get("/api/evolution/concept/{concept_name}")
async def get_evolution_concept_detail(concept_name: str):
    """Full tier: every week's summary plus every week-to-week comparison.
    Generates and caches lazily on first request — meant to back the
    explicit "show concept evolution" action."""
    global engine
    if engine is None:
        raise HTTPException(status_code=400, detail="No documents uploaded yet")
    details = await evolution_data.get_concept_detail(engine, concept_name, client)
    if not details:
        raise HTTPException(status_code=404, detail="Concept not found")
    return details


@app.get("/api/evolution/subgraph/{concept_name}")
async def get_evolution_concept_subgraph(concept_name: str):
    """Get the D3-ready subgraph for a concept's weekly evolution."""
    global engine
    if engine is None:
        raise HTTPException(status_code=400, detail="No documents uploaded yet")
    subgraph = evolution_data.get_concept_subgraph(engine, concept_name)
    if not subgraph:
        raise HTTPException(status_code=404, detail="Concept not found")
    return subgraph


@app.post("/api/evolution/concept/{concept_name}/regenerate")
async def regenerate_evolution_concept(concept_name: str):
    """
    Clears cached summaries/comparisons for a concept so the next request
    for its description or full detail regenerates them from Groq instead
    of reusing whatever's cached.
    """
    global engine
    if engine is None:
        raise HTTPException(status_code=404, detail="No graph loaded")

    occurrences = [c for c in engine.concepts if engine.canonical_nodes.get(c["name"], c["name"]) == concept_name]
    if not occurrences:
        raise HTTPException(status_code=404, detail="Concept not found")

    raw_week_pairs = {(occ["name"], occ["week"]) for occ in occurrences}
    for key in raw_week_pairs:
        engine.concept_week_summaries.pop(key, None)
        engine.degraded_summary_keys.discard(key)
    engine.concept_overall_comparison.pop(concept_name, None)
    engine.concept_consecutive_comparisons.pop(concept_name, None)

    return {"ok": True, "cleared": len(raw_week_pairs)}
 
 
@app.get("/api/quiz")
async def get_quiz(count: int = DEFAULT_QUIZ_QUESTION_COUNT, type: str = "relationship"):
    """
    Generate a quiz from the current graph and material.
 
    type: "relationship" (default, deterministic, no LLM call) or
    "ai" (Groq-generated, grounded in chunk text). A new quiz session is
    created on every call, since question selection is randomized.
    """
    global engine
    if engine is None:
        raise HTTPException(status_code=400, detail="No documents uploaded yet")
 
    if type == "ai":
        questions = await generate_ai_quiz(client, engine, count=count)
    else:
        questions = generate_relationship_quiz(engine, count=count)
 
    if not questions:
        detail = (
            "Not enough relationship data to build a quiz yet."
            if type != "ai" else
            "Not enough material to build an AI quiz yet."
        )
        raise HTTPException(status_code=400, detail=detail)
 
    quiz_id = str(uuid.uuid4())
    engine.quiz_sessions[quiz_id] = {"questions": questions}
    return {"quiz_id": quiz_id, "questions": questions}
 
 
@app.post("/api/quiz/{quiz_id}/submit")
async def submit_quiz(quiz_id: str, submission: QuizSubmission):
    """
    Score a submitted quiz against its stored questions.
 
    Notes: quiz_sessions is in-memory and single-user, same as the rest
    of the engine's state.
    """
    global engine
    if engine is None:
        raise HTTPException(status_code=404, detail="No graph loaded")
 
    session = engine.quiz_sessions.get(quiz_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Quiz session not found")
 
    correct_count, results = score_submission(session["questions"], submission.answers)
    return {
        "score": correct_count,
        "total": len(session["questions"]),
        "results": results,
    }
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)