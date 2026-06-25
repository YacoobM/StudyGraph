"""
Graph JSON Schema and Utilities for StudyGraph

Defines JSON structures for graph nodes, edges, and responses used throughout
the Global Perspective Knowledge Graph Construction pipeline.
"""

from typing import List, Dict, Optional, Any
from enum import Enum

class EdgeType(str, Enum):
    """Enum for edge relationship types"""
    PREREQUISITE = "prerequisite"
    RELATED_TO = "related_to"
    CAUSES = "causes"
    CONSEQUENCE_OF = "consequence_of"
    PART_OF = "part_of"
    CONTAINS = "contains"
    EXPLAINS = "explains"
    EXEMPLIFIES = "exemplifies"
    CONTRADICTS = "contradicts"
    SUPPORTS = "supports"


class NodeStatus(str, Enum):
    """Enum for node processing status"""
    ORIGINAL = "original"
    CANONICAL = "canonical"
    MERGED = "merged"
    INFERRED = "inferred"


GRAPH_NODE_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "Unique identifier (lowercase, underscore-separated)"},
        "name": {"type": "string", "description": "Human-readable concept name"},
        "source": {"type": "string", "description": "Source document filename"},
        "description": {"type": "string", "description": "Optional description of the concept"},
        "is_seed": {"type": "boolean", "description": "Whether this is a seed entity"},
        "is_canonical": {"type": "boolean", "description": "Whether this is a canonical form after merging"},
        "status": {"enum": list(NodeStatus), "description": "Processing status of the node"}
    },
    "required": ["id", "name", "source"]
}

GRAPH_EDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "Unique edge identifier"},
        "source": {"type": "string", "description": "Source node ID"},
        "target": {"type": "string", "description": "Target node ID"},
        "label": {"type": "string", "description": "Relationship label"},
        "relation": {"type": "string", "description": "Relationship type (canonical form)"},
        "conflict_resolved": {"type": "boolean", "description": "Whether this edge resulted from conflict resolution"},
        "is_inferred": {"type": "boolean", "description": "Whether this edge was inferred via transitive reasoning"},
        "source_doc": {"type": "string", "description": "Original source document"}
    },
    "required": ["id", "source", "target", "relation"]
}

GRAPH_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "nodes": {
            "type": "array",
            "items": GRAPH_NODE_SCHEMA,
            "description": "List of concept nodes in the graph"
        },
        "edges": {
            "type": "array",
            "items": GRAPH_EDGE_SCHEMA,
            "description": "List of relationships between nodes"
        },
        "is_fused": {
            "type": "boolean",
            "description": "Whether the graph has been fused/cleaned"
        },
        "stats": {
            "type": "object",
            "properties": {
                "canonical_nodes": {"type": "integer"},
                "fused_edges": {"type": "integer"},
                "inferred_edges": {"type": "integer"}
            }
        }
    },
    "required": ["nodes", "edges"]
}

SEED_GENERATION_SCHEMA = {
    "type": "object",
    "properties": {
        "seed_concepts": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Top 20 most important concepts across the document",
            "maxItems": 20
        }
    },
    "required": ["seed_concepts"]
}

ENTITY_MERGE_SCHEMA = {
    "type": "object",
    "properties": {
        "merges": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "canonical": {"type": "string", "description": "Canonical form of the concept"},
                    "variants": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Semantic variants to merge into canonical"
                    }
                },
                "required": ["canonical", "variants"]
            }
        }
    },
    "required": ["merges"]
}

CONFLICT_RESOLUTION_SCHEMA = {
    "type": "object",
    "properties": {
        "best_relation": {
            "type": "string",
            "description": "Most accurate relation label for conflicting edges"
        }
    },
    "required": ["best_relation"]
}

TRANSITIVE_INFERENCE_SCHEMA = {
    "type": "object",
    "properties": {
        "inferred": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "description": "Index of the 2-hop path"},
                    "should_infer": {"type": "boolean", "description": "Whether to add direct edge"},
                    "relation": {
                        "type": "string",
                        "description": "Inferred relation type if should_infer is true"
                    }
                },
                "required": ["index", "should_infer"]
            }
        }
    },
    "required": ["inferred"]
}

KNOWLEDGE_EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "concepts": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Academic concepts extracted from text"
        },
        "relationships": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "from": {"type": "string", "description": "Source concept"},
                    "to": {"type": "string", "description": "Target concept"},
                    "relation": {"type": "string", "description": "Relationship type"}
                },
                "required": ["from", "to", "relation"]
            }
        }
    },
    "required": ["concepts", "relationships"]
}


def create_node(
    id: str,
    name: str,
    source: str,
    description: str = "",
    is_seed: bool = False,
    is_canonical: bool = False,
    status: str = "original"
) -> Dict[str, Any]:
    """Create a validated graph node object"""
    return {
        "id": id.replace(" ", "_").lower(),
        "name": name,
        "source": source,
        "description": description[:150] if description else "",
        "is_seed": is_seed,
        "is_canonical": is_canonical,
        "status": status
    }


def create_edge(
    source: str,
    target: str,
    relation: str,
    edge_id: Optional[str] = None,
    conflict_resolved: bool = False,
    is_inferred: bool = False,
    source_doc: str = ""
) -> Dict[str, Any]:
    """Create a validated graph edge object"""
    if edge_id is None:
        edge_id = f"{source}_to_{target}"
    
    return {
        "id": edge_id,
        "source": source.replace(" ", "_").lower(),
        "target": target.replace(" ", "_").lower(),
        "label": relation,
        "relation": relation.lower(),
        "conflict_resolved": conflict_resolved,
        "is_inferred": is_inferred,
        "source_doc": source_doc
    }


def create_graph_response(
    nodes: List[Dict],
    edges: List[Dict],
    is_fused: bool = False,
    stats: Optional[Dict] = None
) -> Dict[str, Any]:
    """Create a validated graph response object"""
    if stats is None:
        stats = {
            "canonical_nodes": 0,
            "fused_edges": 0,
            "inferred_edges": 0
        }
    
    return {
        "nodes": nodes,
        "edges": edges,
        "is_fused": is_fused,
        "stats": stats
    }


def validate_json_format(data: Dict, schema: Dict) -> tuple[bool, str]:
    """
    Basic JSON schema validation
    Returns: (is_valid, error_message)
    """
    try:
        required_fields = schema.get("properties", {})
        required = schema.get("required", [])
        
        for field in required:
            if field not in data:
                return False, f"Missing required field: {field}"
        
        for field, value in data.items():
            if field in required_fields:
                field_schema = required_fields[field]
                if "type" in field_schema:
                    if not isinstance(value, eval(field_schema["type"])):
                        return False, f"Field {field} has wrong type"
        
        return True, "Valid"
    except Exception as e:
        return False, str(e)


def merge_node_variants(canonical: str, variants: List[str]) -> Dict[str, str]:
    """Create mapping from variants to canonical form"""
    mapping = {}
    for variant in variants:
        if variant.lower() != canonical.lower():
            mapping[variant.lower().strip()] = canonical.lower().strip()
    return mapping


def apply_canonical_naming(
    nodes: List[Dict],
    canonical_map: Dict[str, str]
) -> List[Dict]:
    """Apply canonical naming to nodes based on merge mapping"""
    result = []
    seen_names = set()
    
    for node in nodes:
        canonical_name = canonical_map.get(node["name"].lower(), node["name"])
        if canonical_name not in seen_names:
            node["name"] = canonical_name
            node["id"] = canonical_name.replace(" ", "_").lower()
            node["is_canonical"] = canonical_name in canonical_map.values()
            result.append(node)
            seen_names.add(canonical_name)
    
    return result


def deduplicate_edges(
    edges: List[Dict],
    conflict_resolution_map: Optional[Dict[tuple, str]] = None
) -> List[Dict]:
    """
    Remove duplicate edges and apply conflict resolution
    conflict_resolution_map: Dict[(from, to)] -> best_relation
    """
    edge_map = {}
    
    for edge in edges:
        key = (edge["source"], edge["target"])
        
        if key in edge_map:
            existing = edge_map[key]
            if existing["relation"] != edge["relation"]:
                if conflict_resolution_map and key in conflict_resolution_map:
                    existing["relation"] = conflict_resolution_map[key]
                    existing["conflict_resolved"] = True
        else:
            edge_map[key] = edge.copy()
    
    return list(edge_map.values())


def annotate_inferred_edges(
    edges: List[Dict],
    inferred_edges: List[Dict]
) -> List[Dict]:
    """Annotate edges with inference metadata"""
    inferred_keys = {(e["source"], e["target"]) for e in inferred_edges}
    
    for edge in edges:
        key = (edge["source"], edge["target"])
        if key in inferred_keys:
            edge["is_inferred"] = True
    
    return edges + inferred_edges
