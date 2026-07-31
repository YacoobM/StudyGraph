import os
import sys
from pathlib import Path
from collections import defaultdict, Counter

# Windows defaults stdout/stderr to the system ANSI codepage (cp1252), not
# UTF-8 — any print() of extracted PDF text hits this the moment a document
# contains a character outside that codepage (e.g. the "fl"/"fi" typographic
# ligatures common in professionally-typeset academic PDFs), crashing the
# whole background upload pipeline with UnicodeEncodeError. errors="replace"
# rather than just switching to utf-8 so an unprintable character degrades
# to "?" instead of still being able to crash a misconfigured terminal.
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from dotenv import load_dotenv
import json
import fitz
from groq import Groq, RateLimitError
from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_similarity
import numpy as np
from datetime import datetime
import time
from bertopic import BERTopic
from sklearn.feature_extraction.text import CountVectorizer
import asyncio

# Ensure the script's directory is on the Python path to resolve import issues.
backend_dir = Path(__file__).resolve().parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))
import evolution_data

load_dotenv()

# globals
# Move outputs directory up one level to prevent `uvicorn --reload` from restarting on file writes.
OUTPUT_DIR = Path(__file__).parent.parent / "outputs"
OUTPUT_DIR.mkdir(exist_ok=True)

# Uploaded PDFs, kept around (keyed by sanitized filename, same identity used
# everywhere else as doc_name) so a concept's source can be opened later —
# previously the bytes were read for text extraction and then discarded.
UPLOADS_DIR = Path(__file__).parent / "uploads"
UPLOADS_DIR.mkdir(exist_ok=True)

app = FastAPI(
    title="StudyGraph API",
    description="FastAPI backend for StudyGraph LLM integration",
    version="0.1.0"
)

app.add_middleware(
    CORSMiddleware,
    # Vite picks the next free port whenever its default is already taken
    # (3000 -> 3001 -> 3002 ...), so a fixed origin list breaks the moment
    # something else is holding the usual port — match any localhost port
    # instead of hardcoding one.
    allow_origin_regex=r"http://localhost:\d+",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

client = Groq(api_key=os.getenv("GROQ_API_KEY"))
embedding_model = SentenceTransformer('all-MiniLM-L6-v2')

engine = None

EXTRACTION_SEMAPHORE = asyncio.Semaphore(5)
EXTRACTION_MAX_RETRIES = 40  # matches evolution_data.py's MAX_RETRIES

class Relationship(BaseModel):
    from_: str = Field(..., alias="from")
    to: str
    relation: str

    class Config:
        populate_by_name = True

class ExtractionResult(BaseModel):
    concepts: list[str]
    relationships: list[Relationship]
    
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


def save_benchmark_results(method_name, timing, seeds):
    """Save benchmark results to text file"""
    try:
        output_file = OUTPUT_DIR / f"seed_generation_{method_name}.txt"
        with open(output_file, "w") as f:
            f.write(f"Method: {method_name.upper()}\n")
            f.write(f"Timestamp: {datetime.now().isoformat()}\n")
            f.write(f"Execution Time: {timing} seconds\n")
            f.write(f"Seeds Generated: {len(seeds)}\n")
            f.write(f"\nSeeds:\n")
            for i, seed in enumerate(seeds, 1):
                f.write(f"{i}. {seed}\n")
        print(f"Saved {method_name} benchmark results to {output_file}")
        return str(output_file)
    except Exception as e:
        print(f"Error saving benchmark results: {e}")
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


class QueryRequest(BaseModel):
    query: str

class RAGResponse(BaseModel):
    answer: str
    citations: list[int]
    citation_label: str
    chunks_used: int
    chunks: list[dict]
    subgraph: dict

class GraphResponse(BaseModel):
    nodes: list[dict]
    edges: list[dict]


class StudyGraphEngine:
    def __init__(self, seed_method="llm"):
        self.concepts = []
        self.edges = []
        self.model = SentenceTransformer('all-MiniLM-L6-v2')
        self.chunk_store = []
        self.graph = None
        self.seed_entities = []         # Top 20 global concepts identified before processing
        self.canonical_nodes = {}       # Map from variants to canonical names (post-fusion)
        self.fused_edges = []           # Edges after conflict resolution (post-fusion)
        self.inferred_edges = []        # Transitive edges inferred during fusion
        self.seed_method = seed_method  # "llm" or "bertopic"
        self.seed_generation_time = None        # float, seconds
        self.seed_generation_method = None      # string, records which method was actually used
        self.upload_latency = None              # float, total time for entire upload endpoint
        self.week_counter = 1                   # Tracks upload batches as "weeks"
        self.uploads_by_week = {}               # Maps week key to filename
        # Unlike chunk_store (wiped on every upload, used only for the RAG chat's
        # "latest doc" search), this accumulates embedded chunks per week so
        # concept-evolution summaries can retrieve grounded source text for any
        # past week, not just the most recently uploaded document.
        self.chunks_by_week = defaultdict(list)
        self._doc_concept_names = {}            # doc_name -> set of admitted concept names (for max_nodes_per_doc)

        # Single-slot status for whatever background pipeline task is
        # currently running (extraction+evolution-precompute after upload,
        # or comparison-precompute after fuse). One slot, not one per
        # upload — this app has one global engine and no concurrent-user
        # story yet, so a second upload started before the first finishes
        # would overwrite this rather than queue; fine for a single-user
        # dev setup, not safe to assume under concurrent uploads.
        self.pipeline_status = {"stage": "idle"}

        # --- Concept-evolution cache ---
        # Populated eagerly, during the upload/fuse pipeline itself
        # (see upload_pdf and fuse_knowledge_graph), using the same
        # get_concept_detail() that used to run lazily on first click. Once
        # generated, a (concept, week) pair is cached forever — viewing it
        # again, from any endpoint, costs nothing.
        self.concept_week_chunks = {}                        # (name, week) -> retrieved chunk list
        self.concept_week_summaries = {}                      # (name, week) -> LLM summary text
        self.concept_consecutive_comparisons = {}              # canonical concept_name -> [{from_week, to_week, comparison}]
        self.concept_overall_comparison = {}                   # canonical concept_name -> comparison text (first week vs. latest week)
        # (name, week) pairs whose concept_week_summaries entry is the raw-excerpt
        # fallback (Groq generation failed) rather than an actual LLM summary — lets
        # the API flag it and the /regenerate endpoint know what's worth retrying.
        self.degraded_summary_keys = set()

    def generate_seeds(self, all_pages):
        """
        Global Seed Generation dispatcher: Routes to BERTopic or LLM method.
        Records timing and method used for benchmarking.
        """
        start = time.time()
        self._generate_seeds_llm(all_pages)
        self.seed_generation_time = round(time.time() - start, 3)
        self.seed_generation_method = self.seed_method
        print(f"Seed generation ({self.seed_method}): {self.seed_generation_time}s")

    def _generate_seeds_llm(self, all_pages):
        """
        LLM-based seed generation: Uses Groq to identify top 20 concepts.
        Original method — unchanged from initial implementation.
        """
        full_text = " ".join([p.get("text", "") for p in all_pages])
        if not full_text.strip():
            return

        seed_prompt = """Analyze the academic material and identify the TOP 20 most fundamental and important concepts.
These should be core topics that the entire material revolves around. Return as JSON:
{"seed_concepts": ["concept1", "concept2", ..., "concept20"]}
Only return the JSON object, no other text."""

        try:
            response = client.chat.completions.create(
                messages=[
                    {"role": "user", "content": f"{seed_prompt}\n\nMaterial:\n{full_text[:5000]}"}
                ],
                model="llama-3.3-70b-versatile",
                response_format={"type": "json_object"},
            )
            seed_json = json.loads(response.choices[0].message.content)
            self.seed_entities = [s.lower().strip() for s in seed_json.get("seed_concepts", [])]
            print(f"LLM generated {len(self.seed_entities)} seed entities: {self.seed_entities}")
        except Exception as e:
            print(f"Error generating seeds (LLM): {e}")
            self.seed_entities = []

    def _clean_sentences_for_bertopic(self, sentences):
        """
        Remove noise sentences before BERTopic clustering.
        Strips citations, references, headers, page numbers, and other
        PDF extraction artefacts that pollute topic modelling.
        """
        import re
        cleaned = []
        for s in sentences:
            # Drop sentences that are mostly digits (page numbers, years, table data)
            if sum(c.isdigit() for c in s) / max(len(s), 1) > 0.3:
                continue
            # Drop sentences containing common reference/citation noise
            if re.search(
                r'\b(pp\.|arxiv|doi|proceedings|conference|vol\.|isbn|http|www\.|et al|ibid)\b',
                s, re.IGNORECASE
            ):
                continue
            # Drop sentences that are fewer than 6 words (too short to be meaningful)
            if len(s.split()) < 6:
                continue
            # Drop sentences that are mostly uppercase (headers, section titles)
            alpha_chars = [c for c in s if c.isalpha()]
            if alpha_chars and sum(c.isupper() for c in alpha_chars) / len(alpha_chars) > 0.5:
                continue
            # Drop sentences that look like author lists or affiliations
            if re.search(r'\b(university|institute|department|laboratory|corp\.|inc\.)\b', s, re.IGNORECASE):
                continue
            cleaned.append(s)
        print(f"BERTopic sentence cleaning: {len(sentences)} → {len(cleaned)} sentences")
        return cleaned

    def _clean_seed_tokens(self, seeds):
        """
        Remove junk tokens from the raw seed list after BERTopic extraction.
        Kills single characters, letter-space fragments like 'p', 'revenge p',
        and any token that is too short to be a meaningful concept.
        """
        import re
        cleaned = []
        for s in seeds:
            s = s.lower().strip()
            # Drop single characters
            if len(s) <= 3:
                continue
            # Drop tokens that are just a letter followed by a space and more (e.g. "p 45", "revenge p")
            if re.match(r'^[a-z]\s', s) or re.search(r'\s[a-z]$', s):
                continue
            # Drop tokens that are purely numeric or punctuation
            if re.match(r'^[\d\s\.\,\-]+$', s):
                continue
            cleaned.append(s)
        return cleaned

    def _deduplicate_seeds_semantically(self, seeds, threshold=0.85):
        """
        Remove semantically redundant seeds using cosine similarity.
        Prevents variants like 'deep fake', 'deep fakes', 'deepfake' all surviving.
        Keeps the first (highest-scored by BERTopic) representative of each cluster.
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

    def _generate_seeds_bertopic(self, all_pages):
        """
        BERTopic-based seed generation: Uses topic modeling for seed extraction.
        Applies sentence cleaning before clustering and seed cleaning after
        to remove PDF noise and semantically redundant terms.
        Falls back to LLM if document is too short for meaningful clustering.
        """

        # Split all page text into individual sentences
        raw_sentences = []
        for page in all_pages:
            text = page.get("text", "")
            raw_sentences.extend([s.strip() for s in text.split(".") if len(s.strip()) > 20])

        # Clean sentences to remove PDF noise before clustering
        sentences = self._clean_sentences_for_bertopic(raw_sentences)

        # Fallback to LLM if not enough clean sentences remain
        if len(sentences) < 10:
            print("Warning: too few clean sentences for BERTopic, falling back to LLM")
            self._generate_seeds_llm(all_pages)
            return

        try:
            # Custom vectorizer: removes stopwords, captures bigrams, requires min frequency
            vectorizer = CountVectorizer(
                stop_words="english",
                ngram_range=(1, 2),  # captures both unigrams and bigrams like "neural network"
                min_df=2             # term must appear in at least 2 sentences to be considered
            )

            # Reuse self.model — do NOT load a second SentenceTransformer
            topic_model = BERTopic(
                embedding_model=self.model,
                vectorizer_model=vectorizer,
                nr_topics=20
            )
            topics, _ = topic_model.fit_transform(sentences)

            # Extract top 3 representative words/phrases per topic (prefer bigrams)
            raw_seeds = []
            topic_info = topic_model.get_topic_info()
            for topic_id in topic_info["Topic"].values:
                if topic_id == -1:  # BERTopic uses -1 for outlier sentences
                    continue
                topic_words = topic_model.get_topic(topic_id)
                # Prefer multi-word terms (bigrams) first, then unigrams
                bigrams = [w for w, _ in topic_words[:6] if " " in w]
                unigrams = [w for w, _ in topic_words[:6] if " " not in w]
                top_words = (bigrams + unigrams)[:3]
                raw_seeds.extend(top_words)

            # Step 1: Remove junk tokens (single chars, fragments, punctuation)
            cleaned_seeds = self._clean_seed_tokens(raw_seeds)

            # Step 2: Deduplicate exact strings
            deduped_seeds = list(set(cleaned_seeds))

            # Step 3: Semantic deduplication to remove near-duplicate variants
            final_seeds = self._deduplicate_seeds_semantically(deduped_seeds)[:20]

            self.seed_entities = final_seeds
            print(f"BERTopic generated {len(self.seed_entities)} seeds: {self.seed_entities}")
        except Exception as e:
            print(f"Error in BERTopic generation: {e}")
            print("Falling back to LLM method")
            self._generate_seeds_llm(all_pages)

    def _run_both_seed_methods(self, all_pages):
        """
        Run both LLM and BERTopic seed generation on the same pages,
        record timing for each, write benchmark files, and return results.
        Called automatically on every upload.
        """
        results = {}

        # --- LLM method ---
        print("\n=== Benchmarking LLM seed method ===")
        llm_temp = StudyGraphEngine(seed_method="llm")
        llm_temp.generate_seeds(all_pages)
        llm_file = save_benchmark_results("llm", llm_temp.seed_generation_time, llm_temp.seed_entities)
        results["llm"] = {
            "seeds": llm_temp.seed_entities,
            "time_seconds": llm_temp.seed_generation_time,
            "seed_count": len(llm_temp.seed_entities),
            "output_file": llm_file,
        }

        # --- BERTopic method ---
        print("\n=== Benchmarking BERTopic seed method ===")
        bertopic_temp = StudyGraphEngine(seed_method="bertopic")
        bertopic_temp.generate_seeds(all_pages)
        bertopic_file = save_benchmark_results("bertopic", bertopic_temp.seed_generation_time, bertopic_temp.seed_entities)
        results["bertopic"] = {
            "seeds": bertopic_temp.seed_entities,
            "time_seconds": bertopic_temp.seed_generation_time,
            "seed_count": len(bertopic_temp.seed_entities),
            "output_file": bertopic_file,
        }

        # --- Assign seeds from requested method to this engine ---
        chosen = results[self.seed_method]
        self.seed_entities = chosen["seeds"]
        self.seed_generation_time = chosen["time_seconds"]
        self.seed_generation_method = self.seed_method
        save_seed_entities(self.seed_entities)

        # --- Compute comparison metrics ---
        llm_set = set(results["llm"]["seeds"])
        bert_set = set(results["bertopic"]["seeds"])
        common = llm_set & bert_set
        denom = max(len(llm_set), len(bert_set))
        bert_time = results["bertopic"]["time_seconds"]
        llm_time = results["llm"]["time_seconds"]

        if bert_time > 0 and llm_time > 0:
            speedup_val = round(llm_time / bert_time, 2)
            if speedup_val > 1:
                speedup_str = f"BERTopic is {speedup_val}x faster"
            else:
                speedup_str = f"LLM is {round(1 / speedup_val, 2)}x faster"
        else:
            speedup_str = "N/A"

        results["comparison"] = {
            "common_seeds": list(common),
            "only_llm": list(llm_set - bert_set),
            "only_bertopic": list(bert_set - llm_set),
            "common_count": len(common),
            "overlap_percentage": round(len(common) / denom * 100, 1) if denom > 0 else 0,
            "speedup": speedup_str,
        }

        return results

    def extract_text(self, file_bytes, filename=None):
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        pages = [{"page": i + 1, "text": p.get_text()} for i, p in enumerate(doc)]
        return pages

    def get_chunks(self, pages, size=2000, overlap=400):
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
            chunks.append({"text": chunk_text, "pages": spanned_pages})
            start += step

        self.index_chunks(chunks)
        return chunks

    async def extract_knowledge(self, text_batch):
        """
        Extract concepts and relationships asynchronously.
        Validates output against schema before returning.

        Retries on Groq rate limits (honoring Retry-After when present,
        exponential backoff otherwise) instead of failing the chunk
        outright — previously a single 429 here silently dropped that
        chunk's concepts with no retry at all, unlike evolution_data.py's
        LLM calls which already had this treatment.
        """
        async with EXTRACTION_SEMAPHORE:
            if self.seed_entities:
                seed_list = ", ".join(self.seed_entities[:10])
                prompt = f"""Extract COMPREHENSIVE academic concepts and relationships, PRIORITIZING these key entities: {seed_list}.
    Focus on relationships that directly involve these seeds or their immediate dependencies.
    Extract ALL meaningful concepts and relationships in the text - be thorough and find multiple connections.
    Relationship types: prerequisite, causes, part_of, explains, enables, related_to
    Return strict JSON:
    {{"concepts": [], "relationships": [{{"from": "", "to": "", "relation": ""}}]}}
    Only return valid JSON, no other text."""
            else:
                prompt = """Extract COMPREHENSIVE academic concepts and relationships from the text.
    Find and include ALL meaningful concepts and relationships - be thorough.
    Include relationship types: prerequisite, causes, part_of, explains, enables, related_to
    Return JSON:
    {"concepts": [], "relationships": [{"from": "", "to": "", "relation": ""}]}
    Only return valid JSON, no other text."""

            def call_groq():
                return client.chat.completions.create(
                    messages=[
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": text_batch},
                    ],
                    model="llama-3.3-70b-versatile",
                    response_format={"type": "json_object"},
                )

            for attempt in range(EXTRACTION_MAX_RETRIES + 1):
                raw_data = None
                try:
                    chat_completion = await asyncio.to_thread(call_groq)
                    raw_data = json.loads(chat_completion.choices[0].message.content)

                    # Validate against schema
                    validated = ExtractionResult(**raw_data)

                    return {
                        "concepts": validated.concepts,
                        "relationships": [
                            {"from": r.from_, "to": r.to, "relation": r.relation}
                            for r in validated.relationships
                        ]
                    }
                except RateLimitError as e:
                    if attempt == EXTRACTION_MAX_RETRIES:
                        print(f"Extraction rate limit exceeded after {EXTRACTION_MAX_RETRIES} retries — dropping this chunk.")
                        return {"concepts": [], "relationships": []}
                    retry_after = None
                    try:
                        retry_after = float(e.response.headers.get("retry-after"))
                    except (AttributeError, TypeError, ValueError):
                        pass
                    delay = retry_after if retry_after is not None else min(60, 5 * (3 ** attempt))
                    print(f"Extraction rate limited (attempt {attempt + 1}/{EXTRACTION_MAX_RETRIES}) — retrying in {delay:.1f}s.")
                    await asyncio.sleep(delay)
                except ValueError as e:
                    print(f"Extraction validation error: {e}")
                    print(f"Raw data was: {raw_data}")
                    return {"concepts": [], "relationships": []}
                except Exception as e:
                    print(f"Error calling Groq: {e}")
                    print(f"Raw data was: {raw_data}")
                    return {"concepts": [], "relationships": []}
            return {"concepts": [], "relationships": []}

    def add_to_graph(self, raw_data, doc_name, week_key, max_nodes_per_doc=None, pages=None):
        if not raw_data:
            return
        for c in raw_data.get("concepts", []):
            if not isinstance(c, str):
                continue
            name = c.lower().strip()
            if max_nodes_per_doc is not None:
                # Cap distinct concept names per document — duplicates of an
                # already-admitted name are still recorded (they're free,
                # not another thing that needs an LLM summary later), only
                # brand-new names for this doc are rejected past the cap.
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
            if from_node and to_node:
                self.edges.append(
                    {"from": from_node, "to": to_node, "relation": relation, "source": doc_name}
                )

    def prune_low_value_concepts(self, doc_name, week_key, min_mentions=2):
        """
        Drop concepts admitted from this doc/week that show no sign of being
        a "key" concept: mentioned fewer than min_mentions times across this
        doc's chunks, and not one of the curated seed_entities. Run this
        before evolution-summary precompute so one-off extraction noise (the
        "extract ALL concepts" prompt is deliberately over-inclusive)
        doesn't turn into wasted LLM summary/comparison calls — fewer
        low-value concepts means fewer Groq calls per upload, which is the
        main lever against hitting the rate limit.

        Previously this also spared any concept with at least one edge, but
        the extraction prompt asks the LLM to find "multiple connections"
        for everything it names, so nearly every concept ends up with an
        edge — that exemption was swallowing the mention-count filter almost
        entirely (observed: raising min_mentions from 2 to 3 pruned only a
        handful of concepts out of dozens). Mention count and seed status
        are now the only ways to survive; a concept mattering enough to have
        an edge but not enough to be mentioned more than once is exactly the
        kind of marginal, thinly-grounded node this filter exists to cut.
        """
        doc_week_concepts = [c for c in self.concepts if c["source"] == doc_name and c["week"] == week_key]
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
            if not (c["source"] == doc_name and c["week"] == week_key and c["name"] in dropped)
        ]
        self.edges = [
            e for e in self.edges
            if not (e["source"] == doc_name and (e["from"] in dropped or e["to"] in dropped))
        ]
        if doc_name in self._doc_concept_names:
            self._doc_concept_names[doc_name] -= dropped

        print(f"Pruned {len(dropped)} low-value concept(s) from {doc_name} ({week_key}): {sorted(dropped)}")

    def fuse_graph(self):
        """
        Global Knowledge Fusion: Called after all files are processed.
        Performs three sub-operations:
        1. Entity Merging: Merge semantically identical nodes
        2. Conflict Resolution: Select accurate relations for multi-edge scenarios
        """
        print("Starting Global Knowledge Fusion...")
        self._merge_entities()
        self._resolve_conflicts()

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

        # Encode all concept names
        embeddings = self.model.encode(node_names)
        
        # Track which nodes have been merged
        merged = set()
        threshold = 0.80  # tune this based on false positives you see
        
        for i in range(len(node_names)):
            if node_names[i] in merged:
                continue
            
            # Find all similar nodes to this one
            sims = cosine_similarity([embeddings[i]], embeddings)[0]
            
            for j in range(i + 1, len(node_names)):
                if node_names[j] in merged:
                    continue
                
                if sims[j] >= threshold:
                    # Merge j into i (i is canonical)
                    self.canonical_nodes[node_names[j]] = node_names[i]
                    merged.add(node_names[j])

        print(f"Entity merging: {len(self.canonical_nodes)} merges via cosine similarity (threshold={threshold})")
        
    def _normalize_relation(self, relation):
        """
        Normalize relation labels to a fixed vocabulary.
        Handles common LLM output variance without an LLM call.
        """
        r = relation.lower().strip()
        
        # Define synonym groups → canonical form
        synonyms = {
            # prerequisite family
            "prerequisite": ["prerequisite", "prerequisite for", "prerequisite_for", "requires", "required for", "depends on"],
            # causation family
            "causes": ["causes", "cause", "leads to", "results in", "produces"],
            # composition family
            "part_of": ["part of", "part_of", "component of", "composed of", "comprises"],
            # relation/connection family
            "related_to": ["related to", "related_to", "associated with", "connected to", "linked to", "relates to"],
            # explanation family
            "explains": ["explains", "explain", "defined as", "describes", "clarifies"],
            # enabling/facilitation
            "enables": ["enables", "enable", "allows", "permits", "facilitates"],
        }
        
        for canonical, variants in synonyms.items():
            if r in variants:
                return canonical
        
        # Fallback: return as-is if not in map
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
        
        # Group edges by canonical node pairs
        for edge in self.edges:
            from_node = self.canonical_nodes.get(edge["from"], edge["from"])
            to_node = self.canonical_nodes.get(edge["to"], edge["to"])
            
            # Normalize the relation label
            normalized_relation = self._normalize_relation(edge["relation"])
            
            key = (from_node, to_node)
            if key not in edge_groups:
                edge_groups[key] = []
            
            edge_groups[key].append({
                "from": from_node,
                "to": to_node,
                "relation": normalized_relation,
                "source": edge.get("source", "unknown"),
                "original_relation": edge["relation"]  # keep for debugging
            })

        # Process each edge group
        for (from_node, to_node), edges in edge_groups.items():
            if len(edges) == 1:
                # Single edge, keep as-is
                self.fused_edges.append({
                    "from": edges[0]["from"],
                    "to": edges[0]["to"],
                    "relation": edges[0]["relation"],
                    "source": edges[0]["source"],
                    "conflict_resolved": False
                })
            else:
                # Multiple edges between same nodes
                relations = [e["relation"] for e in edges]
                
                if len(set(relations)) == 1:
                    # All have the same (normalized) relation — just one edge needed
                    self.fused_edges.append({
                        "from": from_node,
                        "to": to_node,
                        "relation": relations[0],
                        "source": edges[0]["source"],
                        "conflict_resolved": True,  # flag that we collapsed multiple edges
                        "original_count": len(edges)  # how many we merged
                    })
                else:
                    # Different relations even after normalization — rare, pick the first
                    # (Could log this for inspection if needed)
                    self.fused_edges.append({
                        "from": from_node,
                        "to": to_node,
                        "relation": relations[0],
                        "source": edges[0]["source"],
                        "conflict_resolved": True,
                        "original_count": len(edges),
                        "conflicting_relations": relations  # log what the conflict was
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

    def index_chunks(self, chunks):
        self.chunk_store = []
        texts, pages_list = [], []
        for chunk in chunks:
            if isinstance(chunk, dict):
                texts.append(chunk["text"])
                pages_list.append(chunk.get("pages", [chunk["page"]] if "page" in chunk else []))
            else:
                texts.append(chunk)
                pages_list.append([])

        embeddings = self.model.encode(texts, show_progress_bar=True)
        for text, pages, embedding in zip(texts, pages_list, embeddings):
            self.chunk_store.append({"text": text, "pages": pages, "embedding": embedding})
        print(f"Indexed {len(self.chunk_store)} chunks")

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

    def retrieve_chunks(self, query, top_k=3):
        if not self.chunk_store:
            return []
        query_embedding  = self.model.encode([query.lower()])
        chunk_embeddings = np.array([c["embedding"] for c in self.chunk_store])
        similarities     = cosine_similarity(query_embedding, chunk_embeddings)[0]
        top_indices      = np.argsort(similarities)[-top_k:][::-1]
        
        return [
            {
                "text":       self.chunk_store[i]["text"],
                "pages":      self.chunk_store[i]["pages"],
                "similarity": round(float(similarities[i]), 3),
            }
            for i in top_indices
            if similarities[i] > 0.2  # lowered threshold from 0.4
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
        
        # If no text chunks found, return early
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

        # Build context from retrieved chunks with visible citations
        context_blocks, all_cited_pages = [], []
        for r in retrieved:
            citation = self.format_citation(r["pages"])
            header = f"[{citation}]" if citation else "[unknown page]"
            # Truncate chunk text to keep prompt reasonable
            chunk_text = r["text"][:400]
            context_blocks.append(f"{header}\n{chunk_text}")
            all_cited_pages.extend(r["pages"])

        context_text = "\n\n".join(context_blocks)
        unique_pages = sorted(set(all_cited_pages))

        # Optional: add graph context if available
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
                for e in subgraph.get("edges", [])[:5]:  # limit to 5 edges
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
                model="llama-3.3-70b-versatile",
            )
            answer = response.choices[0].message.content
        except Exception as e:
            print(f"Error in RAG query: {e}")
            answer = "Error processing query"

        return {
            "answer": answer,
            "citations": unique_pages,
            "citation_label": self.format_citation(unique_pages),
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


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health_check():
    return {"status": "ok"}


async def _run_upload_pipeline(engine, pages, filename, week_key, max_nodes, upload_start, min_mentions=2):
    """
    Everything slow and LLM-dependent about an upload: seed generation (first
    upload only), chunk extraction, graph building, and — for every concept
    that survives pruning — its cheap-tier description (see
    evolution_data.precompute_week_summaries), so a node's DESCRIPTION and
    SOURCES are both already sitting in cache by the time this pipeline
    reports "done," rather than firing a live Groq call the first time
    someone clicks it. Runs as a background task with no HTTP request
    attached, so Groq rate-limit retries/backoff can take as long as they
    need without risking a request timeout or leaving the browser hanging on
    a spinner for minutes — the same freedom a standalone offline script
    would have.

    The full evolution tier (every week's summary + every week-to-week
    comparison — see get_concept_detail) is deliberately NOT precomputed
    here — it's the expensive tier, gated behind the explicit "View Full
    Evolution" action, since most concepts a document ever surfaces are
    never asked for their full cross-week history.
    """
    try:
        if not engine.seed_entities:  # Only generate seeds on the first upload
            engine.generate_seeds(pages)
            save_seed_entities(engine.seed_entities)

        chunks = engine.get_chunks(pages)
        engine.index_chunks_for_week(chunks, week_key, filename)
        extraction_tasks = [
            engine.extract_knowledge(chunk["text"])
            for chunk in chunks[:15]
        ]
        results = await asyncio.gather(*extraction_tasks)

        # Add to graph sequentially (order doesn't matter here). results[i]
        # came from chunks[:15][i], so chunks[i]'s spanned pages line up with it.
        for i, data in enumerate(results):
            engine.add_to_graph(data, filename, week_key, max_nodes_per_doc=max_nodes, pages=chunks[i].get("pages"))
            print(f"Processed chunk {i + 1}/{len(results)}")

        # Filter out one-off, unconnected, non-seed concepts before they
        # ever reach the graph or get a chance to generate an evolution
        # summary later — see prune_low_value_concepts.
        engine.prune_low_value_concepts(filename, week_key, min_mentions=min_mentions)

        # Description precompute: one cheap-tier summary per surviving
        # concept from this document, batched (see BATCH_SIZE in
        # evolution_data) rather than the one-call-per-click this used to
        # be. Fusion hasn't run yet at this point in the pipeline, so raw
        # names here are genuinely distinct concepts, not spelling variants
        # of one canonical concept — no dedup pass needed (contrast
        # get_concept_detail, which dedupes post-fusion spellings).
        doc_week_pairs = {
            (c["name"], c["week"]) for c in engine.concepts
            if c["source"] == filename and c["week"] == week_key
        }
        await evolution_data.precompute_week_summaries(engine, client, doc_week_pairs)

        # Save current graph state (for debugging/persistence)
        working_edges = engine.fused_edges if engine.fused_edges else engine.edges
        graph_save_path = save_graph_json(
            [{"name": c["name"], "source": c["source"]} for c in engine.concepts],
            working_edges
        )

        engine.upload_latency = round(time.time() - upload_start, 3)
        engine.pipeline_status = {
            "stage": "done",
            "operation": "upload",
            "week": week_key,
            "filename": filename,
            "chunks": len(chunks),
            "seeds_generated": len(engine.seed_entities),
            "total_concepts": len(engine.concepts),
            "total_edges": len(working_edges),
            "graph_saved_to": graph_save_path,
            "error": None,
        }
    except Exception as e:
        print(f"Background upload pipeline failed for {filename}: {e}")
        engine.pipeline_status = {
            "stage": "error", "operation": "upload",
            "week": week_key, "filename": filename, "error": str(e),
        }


@app.post("/api/upload")
async def upload_pdf(file: UploadFile = File(...), seed_method: str = "llm", max_nodes: int = None, min_mentions: int = 2):
    """
    Upload a PDF. Only the fast, local, deterministic steps (read the file,
    parse its text) happen before this responds — extraction, graph
    building, and evolution-summary generation all happen afterward in a
    background task (see _run_upload_pipeline), so this endpoint always
    returns quickly regardless of how much Groq rate-limit backoff the
    actual pipeline ends up needing. Poll GET /api/upload/status for
    progress; the response here is just an acknowledgment, not the result.

    max_nodes: optional hard cap on distinct concepts admitted from this
    document, regardless of quality.

    min_mentions: "key concept" threshold — a concept extracted fewer than
    this many times from the document, and not one of the curated
    seed_entities, is dropped before it reaches the graph or generates an
    evolution summary. Raise this to cut down on low-value nodes and Groq
    calls further; set to 1 to disable pruning entirely.
    """
    global engine
    upload_start = time.time()

    if engine is None:
        engine = StudyGraphEngine(seed_method=seed_method)

    # Strip any directory components a client could send (path traversal) —
    # this sanitized name is used as doc_name everywhere below, consistent
    # with how the rest of the app already treats filename as the document's
    # identity (there's no separate document-id scheme).
    safe_filename = Path(file.filename).name

    try:
        file_bytes = await file.read()
        pages = engine.extract_text(file_bytes, safe_filename)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Persisted so a concept's source can be opened later (see
    # GET /api/documents/file/{filename}) — previously these bytes were
    # discarded right after text extraction.
    (UPLOADS_DIR / safe_filename).write_bytes(file_bytes)

    # Assigned synchronously (not inside the background task) so a second
    # upload started before this one finishes still gets a distinct week.
    current_week_key = f"Week {engine.week_counter}"
    engine.week_counter += 1
    engine.uploads_by_week[current_week_key] = safe_filename

    engine.pipeline_status = {
        "stage": "running", "operation": "upload",
        "week": current_week_key, "filename": safe_filename, "error": None,
    }
    asyncio.create_task(
        _run_upload_pipeline(engine, pages, safe_filename, current_week_key, max_nodes, upload_start, min_mentions)
    )

    return {
        "status": "processing",
        "message": f"Received {len(pages)} pages — processing in the background.",
        "week": current_week_key,
        "filename": safe_filename,
    }


@app.get("/api/upload/status")
async def get_pipeline_status():
    """Poll target for the background upload/fuse pipeline (see StudyGraphEngine.pipeline_status)."""
    global engine
    if engine is None:
        return {"stage": "idle"}
    return engine.pipeline_status


@app.get("/api/documents/file/{filename}")
async def get_document_file(filename: str):
    """Serves a previously-uploaded PDF back (e.g. for the 'open source at page N' viewer)."""
    safe_name = Path(filename).name  # no path traversal via the URL param either
    pdf_path = UPLOADS_DIR / safe_name
    if not pdf_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(pdf_path, media_type="application/pdf", filename=safe_name)


def _source_page_fields(pages):
    """
    pages: sorted list of distinct page ints a concept appeared on within one
    source file. Returns the jump target for the PDF viewer (first page) plus
    a compact human-readable label ("43", "43-48", "43, 47, 50") — mirrors
    StudyGraphEngine.format_citation's range-compaction but without its
    "p./pp." prefix, since the frontend supplies its own "Pg." label.
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
    """Get current knowledge graph (uses fused graph if available)"""
    global engine
    if engine is None:
        return GraphResponse(nodes=[], edges=[])

    # --- Color-coding Logic ---
    # Palette lives in evolution_data so the main graph, the evolution graph,
    # and the per-concept timeline subgraph all agree on what each color means.
    week_color_map = evolution_data.get_week_color_map(engine)
    # --- End Color-coding Logic ---

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

    nodes = []
    # Use a dictionary to aggregate concepts by name, as they can appear multiple times
    aggregated_concepts = {}
    for c in engine.concepts:
        canonical_name = engine.canonical_nodes.get(c["name"], c["name"])
        if canonical_name not in aggregated_concepts:
            aggregated_concepts[canonical_name] = {"sources": defaultdict(set), "weeks": set()}
        # Union pages across every chunk this concept was pulled from for this
        # file — the same concept can turn up in several chunks of one doc.
        aggregated_concepts[canonical_name]["sources"][c["source"]].update(
            p for p in (c.get("pages") or []) if isinstance(p, int)
        )
        # Defensive check: Only add the week if it exists on the concept object.
        # This prevents crashes and incorrect counts if data is malformed.
        week = c.get("week")
        if week:
            aggregated_concepts[canonical_name]["weeks"].add(week)

    for name, data in aggregated_concepts.items():
        appears_in_weeks = len(data["weeks"])
        node_color = evolution_data.DEFAULT_COLOR

        if appears_in_weeks > 1:
            node_color = evolution_data.MULTI_WEEK_COLOR
        elif appears_in_weeks == 1:
            first_week = list(data["weeks"])[0]
            node_color = week_color_map.get(first_week, evolution_data.DEFAULT_COLOR)

        # Now create the final node object for the response
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
                    "conflict_resolved": False,
                    "is_inferred": True
                })

    graph_save_path = save_graph_json(nodes, edges, {
        "canonical_nodes": len(engine.canonical_nodes),
        "fused_edges": len(engine.fused_edges),
        "inferred_edges": len(engine.inferred_edges)
    })

    # Legend: tells the frontend what each color on the graph means, so it
    # doesn't have to hardcode (and risk drifting from) this palette.
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
    global engine
    if engine is None:
        raise HTTPException(status_code=400, detail="No documents uploaded yet")

    try:
        result = engine.rag_query(request.query)
        return result
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/fuse")
async def fuse_knowledge_graph():
    """
    Trigger the Global Knowledge Fusion pipeline (Phase 3 of BATCH-STREAM-BATCH).
    Call this endpoint after all documents are uploaded. Entity merging and
    conflict resolution (both deterministic, no LLM calls) are the only work
    here, so this runs and returns fully synchronously — there's no
    background phase to poll for anymore. Cross-week comparisons only
    become meaningful once fusion has decided which raw names are the same
    canonical concept, but they're no longer precomputed for every concept
    here; they're generated on demand per-concept when a user explicitly
    asks to see that concept's evolution (see evolution_data.get_concept_detail).
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
            "message": "Knowledge graph fusion complete.",
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


@app.get("/api/evolution/graph")
async def get_evolution_graph_data(week: str = None):
    """Get the main evolution graph data, optionally filtered by week."""
    return evolution_data.get_graph_data(engine, week)


@app.get("/api/evolution/concept/{concept_name}/description")
async def get_evolution_concept_description(concept_name: str):
    """Cheap tier: a single grounded summary from the concept's
    earliest-appearance week only. Meant to back a plain node click in
    either graph view — see get_concept_description."""
    description = await evolution_data.get_concept_description(engine, concept_name, client)
    if not description:
        raise HTTPException(status_code=404, detail="Concept not found")
    return description


@app.get("/api/evolution/concept/{concept_name}")
async def get_evolution_concept_detail(concept_name: str):
    """Full tier: every week's summary plus every week-to-week comparison.
    Generates and caches lazily on first request — meant to back the
    explicit "show concept evolution" action, not a plain node click (see
    get_concept_description for that cheaper tier). See get_concept_detail."""
    details = await evolution_data.get_concept_detail(engine, concept_name, client)
    if not details:
        raise HTTPException(status_code=404, detail="Concept not found")
    return details


@app.get("/api/evolution/subgraph/{concept_name}")
async def get_evolution_concept_subgraph(concept_name: str):
    """Get the D3-ready subgraph for a concept's weekly evolution."""
    subgraph = evolution_data.get_concept_subgraph(engine, concept_name)
    if not subgraph:
        raise HTTPException(status_code=404, detail="Concept not found")
    return subgraph


@app.post("/api/evolution/concept/{concept_name}/regenerate")
async def regenerate_evolution_concept(concept_name: str):
    """
    Clears cached summaries/comparisons for a concept so the next request
    for its description or full detail regenerates them from Groq instead
    of reusing whatever's cached — the only way to recover a concept whose
    summary permanently stuck as the raw-excerpt fallback (see
    precompute_week_summaries) or that's just wrong for any other reason.
    Retrieval (concept_week_chunks) is left alone since it's free/local and
    unaffected by why the summary was bad.
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


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)