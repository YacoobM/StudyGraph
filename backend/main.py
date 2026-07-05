from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
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
import asyncio
from chromadb import Client
from schemas import *

load_dotenv()

chroma = Client()

if not os.getenv("GROQ_API_KEY"):
    raise RuntimeError("GROQ_API_KEY is not configured!")

# globals
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

engine = None

EXTRACTION_SEMAPHORE = asyncio.Semaphore(5)
    
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

    def generate_seeds(self, all_pages):
        """
        Global Seed Generation dispatcher: Routes to BERTopic or LLM method.
        Records timing and method used for benchmarking.
        """
        start = time.time()
        self._generate_seeds_llm(all_pages)
        self.seed_generation_time = round(time.time() - start, 3)

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

            try:
                # Run sync Groq call in thread pool
                def call_groq():
                    return client.chat.completions.create(
                        messages=[
                            {"role": "system", "content": prompt},
                            {"role": "user", "content": text_batch},
                        ],
                        model="llama-3.3-70b-versatile",
                        response_format={"type": "json_object"},
                    )
                
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
            except ValueError as e:
                print(f"Extraction validation error: {e}")
                print(f"Raw data was: {raw_data}")
                return {"concepts": [], "relationships": []}
            except Exception as e:
                print(f"Error calling Groq: {e}")
                print(f"Raw data was: {raw_data if 'raw_data' in locals() else 'N/A'}")
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
        #store into the vector database
        texts = [chunk["text"] for chunk in chunks]

        embeddings = self.model.encode(texts)
        
        self.collections.add(
            ids=[f"chunk_{i}" for i in range(len(texts))],
            embeddings=embeddings,
            documents=texts,
            metadatas=[{"pages": chunk["pages"]} for chunk in chunks]
        )

    def retrieve_chunks(self, query, top_k=3):
        query_embedding  = self.model.encode([query.lower()])
        
        results = self.collections.query(
            query_embeddings=query_embedding,
            n_results=top_k
        )
        
        return [
        {
            "text": doc,
            "pages": meta["pages"],
            "similarity": 1 - dist
        }
        for doc, meta, dist in zip(results["documents"][0], results["metadatas"][0], results["distances"][0])
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


# API Endpoints
@app.get("/health")
async def health_check():
    return {"status": "ok"}


@app.post("/api/upload")
async def upload_pdf(file: UploadFile = File(...)):
    """
    Upload a PDF and extract knowledge graph with Global Perspective pipeline.
    On every upload, BOTH seed methods are benchmarked and results written to
    outputs/seed_generation_llm.txt.
    """
    # in case of unconfigured groq api key
    if not os.getenv("GROQ_API_KEY"):
        raise HTTPException(
            status_code=503,
            detail="The AI Service is temporarily down. Please try again later."
        )
    
    global engine
    upload_start = time.time()

    if engine is None:
        #instantitate a new instance of the studygraph class
        engine = StudyGraphEngine(get_collection(1))
    try:
        file_bytes = await file.read()
        pages = engine.extract_text(file_bytes, file.filename)

        engine.generate_seeds(pages)
        save_seed_entities(engine.seed_entities)

        chunks = engine.get_chunks(pages)

        extraction_tasks = [
            engine.extract_knowledge(chunk["text"])
            for chunk in chunks[:15]
        ]
        results = await asyncio.gather(*extraction_tasks)

        # Add to graph sequentially (order doesn't matter here)
        for i, data in enumerate(results):
            engine.add_to_graph(data, file.filename)
            print(f"Processed chunk {i + 1}/{len(results)}")

        # Save current graph state
        working_edges = engine.fused_edges if engine.fused_edges else engine.edges
        graph_save_path = save_graph_json(
            [{"name": c["name"], "source": c["source"]} for c in engine.concepts],
            working_edges
        )

        # Record total upload latency
        engine.upload_latency = round(time.time() - upload_start, 3)

        return {
        "status": "success",
        "message": f"Processed {len(pages)} pages, {min(15, len(chunks))} chunks extracted",
        "chunks": len(chunks),
        "filename": file.filename,
        "seeds_generated": len(engine.seed_entities),
        "seed_entities": engine.seed_entities,
        "pipeline_phase": "stream",
        "graph_saved_to": graph_save_path,
        "raw_concepts": len(engine.concepts),
        "total_edges": len(working_edges)
        }
        
    except HTTPException as e:
        if e.status_code in [401, 402, 403, 429]:
            # This is an error on our end while accessing the groq api
            raise HTTPException(
                status_code=503, 
                detail="The service is temporarily down. Please try again soon."
                )
        elif e.status_code in [413]:
            raise HTTPException(
                status_code=413, 
                detail="The file size is too large, consider uploading the file in different batches."
            )
    
    except Exception as e:
        print(f"Upload error: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
            
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
                "source": [{"file": c["source"]}],  # format as object with file
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
    # in case of unconfigured groq api key
    if not os.getenv("GROQ_API_KEY"):
        raise HTTPException(
            status_code=503,
            detail="The AI Service is temporarily down. Please try again later."
        ) 
    global engine
    if engine is None:
        raise HTTPException(status_code=400, detail="No documents uploaded yet")
    
    # need to review this section again (status codes)
    try:
        result = engine.rag_query(request.query)
        return result
    except HTTPException as e:
        # This is an error on our end while accessing the groq api
        if e.status_code in [401, 402, 403, 429]:
            raise HTTPException(
                status_code=503, 
                detail="The service is temporarily down. Please try again soon."
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

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)