#!/usr/bin/env python3
"""
catalog_generator.py
====================
Парсит dbt manifest.json и собирает Data Catalog в JSON.

Что собираем:
  - models (узлы resource_type='model'): имя, схема, БД, материалзация,
    описание, колонки (с описаниями), теги, meta, owner, depends_on
  - sources (resource_type='source'): имя, схема, БД, описание, колонки
  - snapshots (resource_type='snapshot'): имя, схема, стратегия
  - tests (resource_type='test'): имя, attached_node, test_metadata
  - edges (lineage): из parent_map — ребра "родитель -> ребёнок"
  - orphan_detection: если в raw_code есть `dbt_schema.<model>` или
    `{{ ref('...') }}` не отражённый в depends_on — добавляем виртуальное ребро

Owner/tags: если в meta узла задан owner — берём оттуда.
Иначе эвристика по имени: stg_* -> staging, fct_* -> analytics,
dim_* -> analytics, mart_* -> analytics, snapshot -> snapshot.

Выход: data/catalog.json
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = PROJECT_ROOT / "manifest.json"
CATALOG_PATH = PROJECT_ROOT / "data" / "catalog.json"

# ---------------------------------------------------------------- heuristics

DEFAULT_OWNER = "pavel.onishchenko"
LAYER_PREFIXES = [
    ("stg_", "staging"),
    ("dim_", "analytics"),
    ("fct_", "analytics"),
    ("mart_", "analytics"),
    ("snapshot", "snapshot"),
]


def guess_layer(name: str) -> str:
    for prefix, layer in LAYER_PREFIXES:
        if name.startswith(prefix):
            return layer
    return "other"


def guess_owner(meta: dict, name: str) -> str:
    if isinstance(meta, dict):
        owner = meta.get("owner")
        if owner:
            return owner
    return DEFAULT_OWNER


def guess_tags(meta: dict, name: str) -> list:
    if isinstance(meta, dict) and meta.get("tags"):
        return list(meta["tags"])
    return [guess_layer(name)]


# ---------------------------------------------------------------- extraction

def extract_columns(columns: dict) -> list:
    """Из dict columns узла делаем список с описаниями и типами."""
    result = []
    for col_name, col_info in (columns or {}).items():
        result.append({
            "name": col_name,
            "description": col_info.get("description") or "",
            "data_type": col_info.get("data_type") or "",
            "meta": col_info.get("meta") or {},
        })
    return result


def extract_model(node: dict) -> dict:
    name = node.get("name", "")
    meta = node.get("meta") or {}
    depends_nodes = (node.get("depends_on") or {}).get("nodes") or []
    return {
        "unique_id": node.get("unique_id"),
        "name": name,
        "resource_type": node.get("resource_type"),
        "database": node.get("database"),
        "schema": node.get("schema"),
        "materialized": (node.get("config") or {}).get("materialized"),
        "description": node.get("description") or "",
        "owner": guess_owner(meta, name),
        "tags": guess_tags(meta, name),
        "layer": guess_layer(name),
        "columns": extract_columns(node.get("columns") or {}),
        "depends_on": depends_nodes,
        "original_file_path": node.get("original_file_path"),
        "raw_code": node.get("raw_code") or "",
    }


def extract_source(node: dict) -> dict:
    name = node.get("name", "")
    meta = node.get("meta") or {}
    return {
        "unique_id": node.get("unique_id"),
        "name": name,
        "resource_type": node.get("resource_type"),
        "database": node.get("database"),
        "schema": node.get("schema"),
        "source_name": node.get("source_name"),
        "description": node.get("description") or "",
        "owner": guess_owner(meta, name),
        "tags": guess_tags(meta, name),
        "columns": extract_columns(node.get("columns") or {}),
        "original_file_path": node.get("original_file_path"),
    }


def extract_snapshot(node: dict) -> dict:
    name = node.get("name", "")
    meta = node.get("meta") or {}
    config = node.get("config") or {}
    return {
        "unique_id": node.get("unique_id"),
        "name": name,
        "resource_type": node.get("resource_type"),
        "database": node.get("database"),
        "schema": node.get("schema"),
        "strategy": config.get("strategy"),
        "unique_key": config.get("unique_key"),
        "check_cols": config.get("check_cols"),
        "description": node.get("description") or "",
        "owner": guess_owner(meta, name),
        "tags": guess_tags(meta, name),
        "columns": extract_columns(node.get("columns") or {}),
        "original_file_path": node.get("original_file_path"),
    }


def extract_test(node: dict) -> dict:
    test_meta = node.get("test_metadata") or {}
    return {
        "unique_id": node.get("unique_id"),
        "name": node.get("name"),
        "resource_type": node.get("resource_type"),
        "attached_node": node.get("attached_node"),
        "test_name": test_meta.get("name"),
        "test_kwargs": test_meta.get("kwargs") or {},
        "severity": (node.get("config") or {}).get("severity"),
    }


# ---------------------------------------------------------------- lineage

def build_edges(parent_map: dict) -> list:
    edges = []
    for child, parents in (parent_map or {}).items():
        for parent in parents:
            edges.append({"source": parent, "target": child})
    return edges


def detect_missing_refs(nodes: dict) -> list:
    """
    Ищем в raw_code модели ссылки на dbt-модели, которых нет в depends_on.
    Это дополнение метаданных: mart_sales_funnel читает dbt_schema.fct_sales
    через raw SQL, но без ref() — dbt не видит связь.
    """
    # map: name -> unique_id для моделей и источников
    by_name = {}
    for uid, n in nodes.items():
        if n.get("resource_type") in ("model", "snapshot"):
            by_name[n.get("name")] = uid

    virtual_edges = []
    ref_pattern = re.compile(r"\{\{\s*ref\(\s*['\"]([^'\"]+)['\"]\s*\)\s*\}\}")
    raw_pattern = re.compile(r"dbt_schema\.([a-zA-Z0-9_]+)")

    for uid, n in nodes.items():
        if n.get("resource_type") not in ("model", "snapshot"):
            continue
        code = n.get("raw_code") or ""
        declared = set((n.get("depends_on") or {}).get("nodes") or [])

        candidates = set(ref_pattern.findall(code))
        # сырые SQL-обращения вида dbt_schema.<name> (игнорируем сам себя)
        for m in raw_pattern.findall(code):
            if m != n.get("name"):
                candidates.add(m)

        for cand_name in candidates:
            target_uid = by_name.get(cand_name)
            if not target_uid:
                continue
            # фильтр мусора (self/self-ref)
            if target_uid == uid:
                continue
            if target_uid in declared:
                continue
            virtual_edges.append({
                "source": target_uid,
                "target": uid,
                "reason": f"raw SQL reference to '{cand_name}' (not declared in depends_on)",
            })
    return virtual_edges


# ---------------------------------------------------------------- main

def main():
    if not MANIFEST_PATH.exists():
        print(f"[!] Не найден manifest.json: {MANIFEST_PATH}")
        sys.exit(1)

    with MANIFEST_PATH.open("r", encoding="utf-8") as f:
        manifest = json.load(f)

    nodes = manifest.get("nodes") or {}
    sources = manifest.get("sources") or {}
    parent_map = manifest.get("parent_map") or {}

    models, snapshots, tests, source_nodes = [], [], [], []

    for uid, node in nodes.items():
        rt = node.get("resource_type")
        if rt == "model":
            models.append(extract_model(node))
        elif rt == "snapshot":
            snapshots.append(extract_snapshot(node))
        elif rt == "test":
            tests.append(extract_test(node))

    for uid, node in sources.items():
        if node.get("resource_type") == "source":
            source_nodes.append(extract_source(node))

    edges = build_edges(parent_map)
    virtual_edges = detect_missing_refs(nodes)

    catalog = {
        "metadata": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source_manifest": str(MANIFEST_PATH.name),
            "dbt_version": (manifest.get("metadata") or {}).get("dbt_version"),
            "project_name": (manifest.get("metadata") or {}).get("project_name"),
            "adapter_type": (manifest.get("metadata") or {}).get("adapter_type"),
            "counts": {
                "models": len(models),
                "snapshots": len(snapshots),
                "tests": len(tests),
                "sources": len(source_nodes),
                "edges": len(edges),
                "virtual_edges": len(virtual_edges),
            },
        },
        "models": sorted(models, key=lambda x: x["name"]),
        "snapshots": sorted(snapshots, key=lambda x: x["name"]),
        "tests": sorted(tests, key=lambda x: x["name"]),
        "sources": sorted(source_nodes, key=lambda x: x["name"]),
        "edges": edges,
        "virtual_edges": virtual_edges,
    }

    CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CATALOG_PATH.open("w", encoding="utf-8") as f:
        json.dump(catalog, f, ensure_ascii=False, indent=2)

    counts = catalog["metadata"]["counts"]
    print("[OK] Каталог собран:")
    for k, v in counts.items():
        print(f"     {k:14s} {v}")
    print(f"[OK] Записан: {CATALOG_PATH}")


if __name__ == "__main__":
    main()
