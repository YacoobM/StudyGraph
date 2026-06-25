from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
import os
import json
import fitz
from groq import Groq
from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_similarity
import numpy as np
from pathlib import Path
from datetime import datetime
import time
from bertopic import BERTopic
from sklearn.feature_extraction.text import CountVectorizer

load_dotenv()

OUTPUT_DIR = Path(__file__).parent / "outputs"
OUTPUT_DIR.mkdir(exist_ok=True)

app = FastAPI(
    title="StudyGraph API",
    description="FastAPI backend for StudyGraph LLM integration",
    version="0.1.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

client = Groq(api_key=os.getenv("GROQ_API_KEY"))
embedding_model = SentenceTransformer('all-MiniLM-L6-v2')

engine = None


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

    def generate_seeds(self, all_pages):
        """
        Global Seed Generation dispatcher: Routes to BERTopic or LLM method.
        Records timing and method used for benchmarking.
        """
        start = time.time()
        if self.seed_method == "bertopic":
            self._generate_seeds_bertopic(all_pages)
        else:
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

    def extract_knowledge(self, text_batch):
        """
        Extract concepts and relationships, with a focus on seed entities if available.
        """
        if self.seed_entities:
            seed_list = ", ".join(self.seed_entities[:10])
            prompt = f"""Extract COMPREHENSIVE academic concepts and relationships, PRIORITIZING these key entities: {seed_list}.
Focus on relationships that directly involve these seeds or their immediate dependencies.
Extract ALL meaningful concepts and relationships in the text - be thorough and find multiple connections.
Relationship types: prerequisite, related_to, causes, enables, influences, composes, explains, contradicts, etc.
Return strict JSON:
{{"concepts": [], "relationships": [{{"from": "", "to": "", "relation": ""}}]}}
Only return valid JSON, no other text."""
        else:
            prompt = """Extract COMPREHENSIVE academic concepts and relationships from the text.
Find and include ALL meaningful concepts and relationships - be thorough.
Include various relationship types: prerequisite, related_to, causes, enables, influences, composes, explains, contradicts, etc.
Return JSON:
{"concepts": [], "relationships": [{"from": "", "to": "", "relation": ""}]}
Only return valid JSON, no other text."""

        try:
            chat_completion = client.chat.completions.create(
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": text_batch},
                ],
                model="llama-3.3-70b-versatile",
                response_format={"type": "json_object"},
            )
            graph_json = json.loads(chat_completion.choices[0].message.content)
            self.graph = graph_json
            return graph_json
        except Exception as e:
            print(f"Error calling Groq: {e}")
            return {"concepts": [], "relationships": []}

    def add_to_graph(self, raw_data, doc_name):
        if not raw_data:
            return
        for c in raw_data.get("concepts", []):
            if not isinstance(c, str):
                continue
            name = c.lower().strip()
            if not any(node["name"] == name for node in self.concepts):
                self.concepts.append({"name": name, "source": doc_name})

        for r in raw_data.get("relationships", []):
            from_node = r.get("from", "").lower().strip()
            to_node   = r.get("to", "").lower().strip()
            relation  = r.get("relation", "").lower().strip()
            if from_node and to_node:
                self.edges.append(
                    {"from": from_node, "to": to_node, "relation": relation, "source": doc_name}
                )

    def fuse_graph(self):
        """
        Global Knowledge Fusion: Called after all files are processed.
        Performs three sub-operations:
        1. Entity Merging: Merge semantically identical nodes
        2. Conflict Resolution: Select accurate relations for multi-edge scenarios
        3. Path Inference: Add inferred transitive relationships
        """
        print("Starting Global Knowledge Fusion...")
        self._merge_entities()
        self._resolve_conflicts()
        self._infer_transitive_paths()

        print(f"Fusion complete. Canonical nodes: {len(self.canonical_nodes)}, "
              f"Fused edges: {len(self.fused_edges)}, Inferred edges: {len(self.inferred_edges)}")

    def _merge_entities(self):
        """
        Entity Merging: Use LLM to identify semantically identical nodes and merge them.
        """
        if not self.concepts:
            return

        node_names = [c["name"] for c in self.concepts]

        merge_prompt = f"""Identify semantically identical or equivalent concepts in this list.
Group synonyms and different names for the same concept together.
Return JSON with canonical forms:
{{"merges": [{{"canonical": "canonical_name", "variants": ["var1", "var2"]}}]}}

Concept list: {json.dumps(node_names)}

Only return valid JSON."""

        try:
            response = client.chat.completions.create(
                messages=[{"role": "user", "content": merge_prompt}],
                model="llama-3.3-70b-versatile",
                response_format={"type": "json_object"},
            )
            merge_data = json.loads(response.choices[0].message.content)

            for merge_group in merge_data.get("merges", []):
                canonical = merge_group.get("canonical", "").lower().strip()
                if canonical:
                    for variant in merge_group.get("variants", []):
                        variant_lower = variant.lower().strip()
                        if variant_lower != canonical:
                            self.canonical_nodes[variant_lower] = canonical

            print(f"Entity merging: {len(self.canonical_nodes)} mappings created")
        except Exception as e:
            print(f"Error during entity merging: {e}")

    def _resolve_conflicts(self):
        """
        Conflict Resolution: When multiple edges exist between the same two nodes
        with different relation labels, use LLM to select the most accurate relation.
        """
        edge_groups = {}
        for edge in self.edges:
            from_node = self.canonical_nodes.get(edge["from"], edge["from"])
            to_node = self.canonical_nodes.get(edge["to"], edge["to"])
            key = (from_node, to_node)
            if key not in edge_groups:
                edge_groups[key] = []
            edge_groups[key].append(edge)

        for (from_node, to_node), edges in edge_groups.items():
            if len(edges) == 1:
                self.fused_edges.append(edges[0])
            else:
                relations = [e["relation"] for e in edges]
                sources = [e["source"] for e in edges]

                if len(set(relations)) > 1:
                    conflict_prompt = f"""Given these relationships between '{from_node}' and '{to_node}':
Relations: {relations}
Sources: {sources}

Select the SINGLE most accurate and general relation that captures the true connection.
Return JSON: {{"best_relation": "relation_name"}}"""

                    try:
                        response = client.chat.completions.create(
                            messages=[{"role": "user", "content": conflict_prompt}],
                            model="llama-3.3-70b-versatile",
                            response_format={"type": "json_object"},
                        )
                        conflict_data = json.loads(response.choices[0].message.content)
                        best_relation = conflict_data.get("best_relation", relations[0]).lower().strip()
                    except Exception as e:
                        print(f"Error resolving conflict for {from_node}->{to_node}: {e}")
                        best_relation = relations[0]
                else:
                    best_relation = relations[0]

                self.fused_edges.append({
                    "from": from_node,
                    "to": to_node,
                    "relation": best_relation,
                    "source": sources[0],
                    "conflict_resolved": len(edges) > 1
                })

        print(f"Conflict resolution: Processed {len(edge_groups)} edge groups, "
              f"created {len(self.fused_edges)} fused edges")

    def _infer_transitive_paths(self):
        """
        Path Inference: Use LLM to infer and add missing transitive relationships.
        """
        if len(self.fused_edges) < 2:
            return

        graph_dict = {}
        for edge in self.fused_edges:
            from_node = edge["from"]
            to_node = edge["to"]
            relation = edge["relation"]
            if from_node not in graph_dict:
                graph_dict[from_node] = []
            graph_dict[from_node].append((to_node, relation))

        candidate_paths = []
        for from_node, neighbors in graph_dict.items():
            for intermediate, rel1 in neighbors:
                if intermediate in graph_dict:
                    for to_node, rel2 in graph_dict[intermediate]:
                        if from_node != to_node:
                            direct_exists = any(
                                e["from"] == from_node and e["to"] == to_node
                                for e in self.fused_edges
                            )
                            if not direct_exists:
                                candidate_paths.append({
                                    "from": from_node,
                                    "to": to_node,
                                    "path": f"{from_node} --[{rel1}]--> {intermediate} --[{rel2}]--> {to_node}"
                                })

        if not candidate_paths:
            return

        paths_batch = candidate_paths[:10]
        paths_str = "\n".join([f"{i+1}. {p['path']}" for i, p in enumerate(paths_batch)])

        infer_prompt = f"""For each 2-hop path, determine if a direct relationship should be inferred.
Return a JSON array indicating which should be added (true/false) and the inferred relation:
{{"inferred": [{{"index": 0, "should_infer": true, "relation": "relation_name"}}, ...]}}

Paths:
{paths_str}

Only return valid JSON."""

        try:
            response = client.chat.completions.create(
                messages=[{"role": "user", "content": infer_prompt}],
                model="llama-3.3-70b-versatile",
                response_format={"type": "json_object"},
            )
            infer_data = json.loads(response.choices[0].message.content)

            for inference in infer_data.get("inferred", []):
                if inference.get("should_infer", False):
                    idx = inference.get("index", 0)
                    if 0 <= idx < len(paths_batch):
                        path_info = paths_batch[idx]
                        self.inferred_edges.append({
                            "from": path_info["from"],
                            "to": path_info["to"],
                            "relation": inference.get("relation", "related_to").lower().strip(),
                            "source": "inferred_transitive",
                            "path": path_info["path"]
                        })

            print(f"Transitive inference: Added {len(self.inferred_edges)} inferred edges")
        except Exception as e:
            print(f"Error during transitive inference: {e}")

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

    def retrieve_chunks(self, query, top_k=3):
        if not self.chunk_store:
            return []
        query_embedding  = self.model.encode([query.lower()])
        chunk_embeddings = np.array([c["embedding"] for c in self.chunk_store])
        similarities     = cosine_similarity(query_embedding, chunk_embeddings)[0]
        top_indices      = np.argsort(similarities)[-top_k:][::-1]
        
        print(
            [
            {
                "text":       self.chunk_store[i]["text"],
                "pages":      self.chunk_store[i]["pages"],
                "similarity": round(float(similarities[i]), 3),
            }
            for i in top_indices
            if similarities[i] > 0.4
        ]
        )
        
        return [
            {
                "text":       self.chunk_store[i]["text"],
                "pages":      self.chunk_store[i]["pages"],
                "similarity": round(float(similarities[i]), 3),
            }
            for i in top_indices
            if similarities[i] > 0.4
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
        Global Perspective RAG Query: Combines vector-based chunk retrieval with
        multi-hop graph search and fused knowledge graph context.
        """
        retrieved = self.retrieve_chunks(query, top_k=3)
        multi_hop_result = self.multi_hop_search(query, depth=2)

        if not retrieved and not multi_hop_result.get("nodes"):
            return {
                "answer": "I couldn't find relevant information in your documents.",
                "citations": [],
                "citation_label": "",
                "chunks_used": 0,
                "chunks": [],
                "subgraph": {"nodes": [], "edges": []},
                "graph_search_depth": 0
            }

        context_blocks, all_cited_pages = [], []
        for r in retrieved:
            citation = self.format_citation(r["pages"])
            header   = f"[{citation}]" if citation else "[unknown page]"
            context_blocks.append(f"{header}\n{r['text']}")
            all_cited_pages.extend(r["pages"])

        context_text = "\n\n".join(context_blocks) if context_blocks else "[No direct text chunks found]"
        unique_pages = sorted(set(all_cited_pages)) if all_cited_pages else []

        working_edges = self.fused_edges if self.fused_edges else self.edges

        if working_edges and self.fused_edges:
            subgraph = {
                "nodes": multi_hop_result.get("nodes", []),
                "edges": multi_hop_result.get("edges", []),
                "is_fused": True
            }
        else:
            original_subgraph = self.get_relevant_subgraph(query)
            subgraph = {
                "nodes": original_subgraph.get("nodes", []),
                "edges": original_subgraph.get("edges", []),
                "is_fused": False
            }

        graph_section = ""
        if subgraph.get("edges"):
            graph_lines = ["FUSED KNOWLEDGE GRAPH STRUCTURE:"]
            graph_lines.append("Related Concepts:")

            seed_nodes = [n for n in subgraph.get("nodes", []) if n.get("is_seed")]
            if seed_nodes:
                seed_names = ", ".join([n["name"] for n in seed_nodes])
                graph_lines.append(f"  Key Seeds: {seed_names}")

            if subgraph.get("edges"):
                graph_lines.append("Concept Connections:")
                for e in subgraph.get("edges", [])[:10]:
                    edge_type = "→" if not e.get("conflict_resolved") else "→*"
                    marker = " (INFERRED)" if e.get("source") == "inferred_transitive" else ""
                    graph_lines.append(f"  {e['from']} {edge_type} [{e['relation']}] {e['to']}{marker}")

            graph_section = "\n".join(graph_lines) + "\n"

        system_prompt = """You are an advanced study assistant powered by a Global Knowledge Graph.
Your task is to answer using BOTH the provided text chunks AND the graph structure.

Instructions:
1. Answer using the RELEVANT_TEXT_CHUNKS when available for primary information
2. Use the FUSED_SUBGRAPH_STRUCTURE to explain HOW concepts are connected
3. Cite page references inline from text chunks
4. Explicitly mention concept relationships from the graph where relevant
5. If text is insufficient, use graph connections to provide context

Format citations as: [p. 3] or [pp. 4-5]"""

        user_prompt = f"""RELEVANT_TEXT_CHUNKS:
{context_text}

{graph_section}

QUESTION: {query}

Provide a comprehensive answer using both text content and concept relationships."""

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
            "answer":         answer,
            "citations":      unique_pages,
            "citation_label": self.format_citation(unique_pages),
            "chunks_used":    len(retrieved),
            "chunks": [
                {
                    "text":       r["text"][:200] + "..." if len(r["text"]) > 200 else r["text"],
                    "pages":      r["pages"],
                    "citation":   self.format_citation(r["pages"]),
                    "similarity": r["similarity"],
                }
                for r in retrieved
            ],
            "subgraph": {
                "nodes": subgraph.get("nodes", []),
                "edges": subgraph.get("edges", []),
                "is_fused": subgraph.get("is_fused", False)
            },
            "graph_search_depth": 2 if self.fused_edges else 0,
            "fusion_status": "fused" if self.fused_edges else "raw"
        }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health_check():
    return {"status": "ok"}


@app.post("/api/upload")
async def upload_pdf(file: UploadFile = File(...), seed_method: str = "llm"):
    """
    Upload a PDF and extract knowledge graph with Global Perspective pipeline.
    On every upload, BOTH seed methods are benchmarked and results written to
    outputs/seed_generation_llm.txt and outputs/seed_generation_bertopic.txt.
    The seed_method parameter controls which method's seeds are actually used
    for the knowledge extraction pipeline.
    """
    global engine
    upload_start = time.time()

    if engine is None:
        engine = StudyGraphEngine(seed_method=seed_method)

    try:
        file_bytes = await file.read()
        pages = engine.extract_text(file_bytes, file.filename)

        # BATCH-STREAM-BATCH PIPELINE
        # Phase 1: BATCH — Run both seed methods, benchmark both, use requested one
        benchmark_results = None
        if not engine.seed_entities:
            benchmark_results = engine._run_both_seed_methods(pages)

        # Phase 2: STREAM — Process chunks with seed-focused extraction
        chunks = engine.get_chunks(pages)

        for i, chunk in enumerate(chunks[:15]):
            data = engine.extract_knowledge(chunk["text"])
            engine.add_to_graph(data, file.filename)
            print(f"Processed chunk {i + 1}/{min(15, len(chunks))}")

        # Save current graph state
        working_edges = engine.fused_edges if engine.fused_edges else engine.edges
        graph_save_path = save_graph_json(
            [{"name": c["name"], "source": c["source"]} for c in engine.concepts],
            working_edges
        )

        # Record total upload latency
        engine.upload_latency = round(time.time() - upload_start, 3)

        # Build benchmark block for response
        if benchmark_results:
            benchmark_block = {
                "seed_method_used": engine.seed_generation_method,
                "seed_generation_time_seconds": engine.seed_generation_time,
                "overall_latency_seconds": engine.upload_latency,
                "llm": {
                    "time_seconds": benchmark_results["llm"]["time_seconds"],
                    "seed_count": benchmark_results["llm"]["seed_count"],
                    "output_file": benchmark_results["llm"]["output_file"],
                },
                "bertopic": {
                    "time_seconds": benchmark_results["bertopic"]["time_seconds"],
                    "seed_count": benchmark_results["bertopic"]["seed_count"],
                    "output_file": benchmark_results["bertopic"]["output_file"],
                },
                "comparison": benchmark_results["comparison"],
            }
        else:
            # Seeds were already populated from a prior upload in this session
            benchmark_block = {
                "seed_method_used": engine.seed_generation_method,
                "seed_generation_time_seconds": engine.seed_generation_time,
                "overall_latency_seconds": engine.upload_latency,
                "note": "Seeds already generated from previous upload; benchmark skipped.",
            }

        return {
            "status": "success",
            "message": f"Processed {len(pages)} pages, {min(15, len(chunks))} chunks extracted",
            "chunks": len(chunks),
            "filename": file.filename,
            "seeds_generated": len(engine.seed_entities),
            "seed_entities": engine.seed_entities,
            "pipeline_phase": "stream",
            "graph_saved_to": graph_save_path,
            "total_concepts": len(engine.concepts),
            "total_edges": len(working_edges),
            "benchmark": benchmark_block,
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/graph")
async def get_graph():
    """Get current knowledge graph (uses fused graph if available)"""
    global engine
    if engine is None:
        return GraphResponse(nodes=[], edges=[])

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
    for c in engine.concepts:
        canonical_name = engine.canonical_nodes.get(c["name"], c["name"])
        if not any(n["name"] == canonical_name for n in nodes):
            nodes.append({
                "id": canonical_name.replace(" ", "_"),
                "name": canonical_name,
                "source": c["source"],
                "description": "; ".join(concept_relations.get(canonical_name, []))[:150] or canonical_name,
                "is_canonical": canonical_name in engine.canonical_nodes.values()
            })

    valid_node_ids = {n["id"] for n in nodes}

    edges = [
        {
            "id": f"{e['from']}_to_{e['to']}",
            "source": e["from"].replace(" ", "_"),
            "target": e["to"].replace(" ", "_"),
            "label": e["relation"],
            "relation": e["relation"],
            "conflict_resolved": e.get("conflict_resolved", False),
            "is_inferred": e.get("source") == "inferred_transitive"
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

    return {
        "nodes": nodes,
        "edges": edges,
        "is_fused": bool(engine.fused_edges),
        "graph_saved_to": graph_save_path,
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



if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)