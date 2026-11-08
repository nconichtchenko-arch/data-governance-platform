#!/usr/bin/env python3
"""
streamlit_app.py
================
Единый дашборд Data Governance для платформы данных.

Три вкладки:
  1. Каталог    — модели, снапшоты, тесты, источники с owner/layer/tags/columns
  2. Контракты  — результаты валидации YAML-контрактов (из contract_results.json)
  3. Lineage    — интерактивный граф зависимостей (dbt + восстановленные рёбра)

Источники данных:
  - data/catalog.json            (сгенерирован scripts/catalog_generator.py)
  - data/contract_results.json   (сгенерирован scripts/contract_validator.py)
  - manifest.json                (для сырых имён узлов, если нужно)

Запуск:
  streamlit run app/streamlit_app.py
"""

import json
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

# ---------------------------------------------------------------- paths

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CATALOG_PATH = PROJECT_ROOT / "data" / "catalog.json"
CONTRACT_RESULTS_PATH = PROJECT_ROOT / "data" / "contract_results.json"

# ---------------------------------------------------------------- helpers

@st.cache_data(show_spinner=False)
def load_catalog() -> dict:
    if not CATALOG_PATH.exists():
        return {}
    with CATALOG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


@st.cache_data(show_spinner=False)
def load_contract_results() -> dict:
    if not CONTRACT_RESULTS_PATH.exists():
        return {}
    with CONTRACT_RESULTS_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def short_id(uid: str) -> str:
    """'model.marketplace_dwh.fct_sales' -> 'fct_sales'"""
    if not uid:
        return ""
    return uid.rsplit(".", 1)[-1]


def status_emoji(status: str) -> str:
    return {"PASS": "✅", "FAIL": "❌", "ERROR": "⚠️"}.get(status, "•")


# ---------------------------------------------------------------- tab 1: catalog

def render_catalog_tab(catalog: dict) -> None:
    st.subheader("Каталог данных")

    if not catalog:
        st.warning("catalog.json не найден. Запусти `python scripts/catalog_generator.py`.")
        return

    meta = catalog.get("metadata", {})
    counts = meta.get("counts", {})

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Модели", counts.get("models", 0))
    c2.metric("Снапшоты", counts.get("snapshots", 0))
    c3.metric("Тесты", counts.get("tests", 0))
    c4.metric("Источники", counts.get("sources", 0))
    c5.metric("Рёбра Lineage", counts.get("edges", 0) + counts.get("virtual_edges", 0))

    st.caption(
        f"Сгенерировано: {meta.get('generated_at', '—')} · "
        f"dbt {meta.get('dbt_version', '?')} · "
        f"project: {meta.get('project_name', '?')} · "
        f"adapter: {meta.get('adapter_type', '?')}"
    )

    st.markdown("---")

    # ---------- filters
    fc1, fc2, fc3 = st.columns([2, 2, 3])
    all_layers = sorted({m.get("layer", "other") for m in catalog.get("models", [])})
    all_owners = sorted({m.get("owner", "") for m in catalog.get("models", [])})
    with fc1:
        layers = st.multiselect("Слой (layer)", all_layers, default=all_layers)
    with fc2:
        owners = st.multiselect("Владелец (owner)", all_owners, default=all_owners)
    with fc3:
        query = st.text_input("Поиск по имени / описанию", "")

    models = [
        m for m in catalog.get("models", [])
        if (m.get("layer") in layers)
        and (m.get("owner") in owners)
        and (
            not query
            or query.lower() in (m.get("name", "") + " " + m.get("description", "")).lower()
        )
    ]

    st.markdown(f"**Модели:** показано {len(models)} из {len(catalog.get('models', []))}")

    if models:
        rows = []
        for m in models:
            rows.append({
                "name": m["name"],
                "layer": m.get("layer"),
                "materialized": m.get("materialized"),
                "owner": m.get("owner"),
                "tags": ", ".join(m.get("tags") or []),
                "columns": len(m.get("columns") or []),
                "depends_on": ", ".join(short_id(d) for d in (m.get("depends_on") or [])),
                "description": (m.get("description") or "").strip().replace("\n", " ")[:120],
            })
        df = pd.DataFrame(rows)
        st.dataframe(df, use_container_width=True, hide_index=True)

        # ---------- details
        st.markdown("#### Детали модели")
        chosen = st.selectbox("Выбери модель", [m["name"] for m in models])
        m = next(x for x in models if x["name"] == chosen)

        d1, d2 = st.columns([1, 1])
        with d1:
            st.markdown(f"**unique_id:** `{m.get('unique_id')}`")
            st.markdown(f"**database.schema:** `{m.get('database')}.{m.get('schema')}`")
            st.markdown(f"**materialized:** `{m.get('materialized')}`")
            st.markdown(f"**owner:** `{m.get('owner')}`")
            st.markdown(f"**tags:** `{', '.join(m.get('tags') or [])}`")
            st.markdown(f"**file:** `{m.get('original_file_path')}`")
        with d2:
            st.markdown("**description:**")
            st.info(m.get("description") or "—")

        st.markdown("**Колонки:**")
        cols = m.get("columns") or []
        if cols:
            cdf = pd.DataFrame([
                {
                    "column": c.get("name"),
                    "type": c.get("data_type") or "—",
                    "description": c.get("description") or "—",
                }
                for c in cols
            ])
            st.dataframe(cdf, use_container_width=True, hide_index=True)
        else:
            st.caption("В schema.yml колонки не описаны.")

        deps = [short_id(d) for d in (m.get("depends_on") or [])]
        st.markdown(f"**depends_on:** {', '.join(f'`{d}`' for d in deps) if deps else '—'}")

    st.markdown("---")

    # ---------- snapshots / tests / sources
    t1, t2, t3 = st.tabs(["Снапшоты", "Тесты", "Источники"])

    with t1:
        snaps = catalog.get("snapshots", [])
        if snaps:
            sdf = pd.DataFrame([
                {
                    "name": s["name"],
                    "strategy": s.get("strategy"),
                    "unique_key": s.get("unique_key"),
                    "check_cols": str(s.get("check_cols")),
                    "schema": s.get("schema"),
                    "file": s.get("original_file_path"),
                }
                for s in snaps
            ])
            st.dataframe(sdf, use_container_width=True, hide_index=True)
        else:
            st.caption("Снапшотов нет.")

    with t2:
        tests = catalog.get("tests", [])
        if tests:
            tdf = pd.DataFrame([
                {
                    "name": t["name"],
                    "test": t.get("test_name"),
                    "attached_to": short_id(t.get("attached_node") or ""),
                    "severity": t.get("severity"),
                    "kwargs": json.dumps(t.get("test_kwargs"), ensure_ascii=False)[:80],
                }
                for t in tests
            ])
            st.dataframe(tdf, use_container_width=True, hide_index=True)
        else:
            st.caption("Тестов нет.")

    with t3:
        sources = catalog.get("sources", [])
        if sources:
            srcdf = pd.DataFrame([
                {
                    "name": s["name"],
                    "source_name": s.get("source_name"),
                    "schema": s.get("schema"),
                    "database": s.get("database"),
                    "file": s.get("original_file_path"),
                }
                for s in sources
            ])
            st.dataframe(srcdf, use_container_width=True, hide_index=True)
        else:
            st.caption("Источников нет.")


# ---------------------------------------------------------------- tab 2: contracts

def render_contracts_tab(results: dict) -> None:
    st.subheader("Контракты данных")

    if not results:
        st.warning(
            "contract_results.json не найден. Запусти `python scripts/contract_validator.py`."
        )
        return

    overall = results.get("overall", "?")
    st.markdown(
        f"**Общий статус:** {status_emoji(overall)} **{overall}**  ·  "
        f"контрактов: {results.get('contracts_count', 0)}  ·  "
        f"проверено: {results.get('generated_at', '—')}"
    )

    for rep in results.get("reports", []):
        rep_overall = rep.get("overall", "?")
        header = (
            f"{status_emoji(rep_overall)} Контракт: "
            f"{rep.get('dataset')}  ({rep.get('schema')}.{rep.get('table')})  —  {rep_overall}"
        )
        with st.expander(header, expanded=True):
            checks = rep.get("checks", [])
            rows = []
            for chk in checks:
                rows.append({
                    "check": chk.get("name"),
                    "status": chk.get("status"),
                    "violations": chk.get("violations") if "violations" in chk else "",
                    "details": chk.get("details") or chk.get("error") or "",
                })
            df = pd.DataFrame(rows)
            st.dataframe(df, use_container_width=True, hide_index=True)

            # summary
            total = len(checks)
            passed = sum(1 for c in checks if c.get("status") == "PASS")
            failed = sum(1 for c in checks if c.get("status") == "FAIL")
            errored = sum(1 for c in checks if c.get("status") == "ERROR")
            cc1, cc2, cc3, cc4 = st.columns(4)
            cc1.metric("Всего проверок", total)
            cc2.metric("PASS", passed)
            cc3.metric("FAIL", failed)
            cc4.metric("ERROR", errored)


# ---------------------------------------------------------------- tab 3: lineage

def render_lineage_tab(catalog: dict) -> None:
    st.subheader("Lineage (граф зависимостей)")

    if not catalog:
        st.warning("catalog.json не найден.")
        return

    edges = catalog.get("edges", [])
    virtual_edges = catalog.get("virtual_edges", [])

    if not edges and not virtual_edges:
        st.info("Граф пуст: нет рёбер ни в parent_map, ни в virtual_edges.")
        return

    # строим узлы и подписи
    node_ids = set()
    for e in edges + virtual_edges:
        node_ids.add(e["source"])
        node_ids.add(e["target"])

    # классификация узлов по префиксу unique_id
    def node_kind(uid: str) -> str:
        if uid.startswith("source."):
            return "source"
        if uid.startswith("model."):
            return "model"
        if uid.startswith("snapshot."):
            return "snapshot"
        if uid.startswith("test."):
            return "test"
        return "other"

    kind_colors = {
        "source": "#8c8c8c",
        "model": "#1f77b4",
        "snapshot": "#9467bd",
        "test": "#ff7f0e",
        "other": "#2ca02c",
    }

    labels = {uid: short_id(uid) for uid in node_ids}
    kinds = {uid: node_kind(uid) for uid in node_ids}

    # простой layout: по "слоям" (source -> staging -> analytics)
    # используем Kahn-like: sources слева, тесты справа
    layer_order = {"source": 0, "model": 1, "snapshot": 2, "test": 3, "other": 4}

    # координаты: считаем "глубину" через топологическую сортировку по parent_map
    from collections import defaultdict, deque

    # граф: parent -> children
    children = defaultdict(set)
    parents = defaultdict(set)
    for e in edges:
        children[e["source"]].add(e["target"])
        parents[e["target"]].add(e["source"])

    # depth = длина самого длинного пути от источника
    depth = {}
    queue = deque([n for n in node_ids if not parents[n]])
    for n in queue:
        depth[n] = 0
    in_degree = {n: len(parents[n]) for n in node_ids}
    # Kahn
    q2 = deque([n for n in node_ids if in_degree[n] == 0])
    while q2:
        n = q2.popleft()
        for ch in children[n]:
            depth[ch] = max(depth.get(ch, 0), depth.get(n, 0) + 1)
            in_degree[ch] -= 1
            if in_degree[ch] == 0:
                q2.append(ch)

    # x = depth, y = порядковый внутри depth
    by_depth = defaultdict(list)
    for uid in node_ids:
        by_depth[depth.get(uid, 0)].append(uid)

    pos = {}
    for d, nodes in by_depth.items():
        # стабильная сортировка по kind и имени
        nodes_sorted = sorted(nodes, key=lambda u: (layer_order.get(kinds[u], 9), labels[u]))
        for i, uid in enumerate(nodes_sorted):
            pos[uid] = (d, -i)

    # -------- рисуем рёбра (прямые)
    edge_x, edge_y = [], []
    for e in edges:
        x0, y0 = pos[e["source"]]
        x1, y1 = pos[e["target"]]
        edge_x += [x0, x1, None]
        edge_y += [y0, y1, None]

    # -------- рисуем виртуальные рёбра (пунктир)
    vedge_x, vedge_y = [], []
    for e in virtual_edges:
        if e["source"] not in pos or e["target"] not in pos:
            continue
        x0, y0 = pos[e["source"]]
        x1, y1 = pos[e["target"]]
        vedge_x += [x0, x1, None]
        vedge_y += [y0, y1, None]

    # -------- узлы
    node_x, node_y, node_text, node_color, node_hover = [], [], [], [], []
    for uid in node_ids:
        x, y = pos[uid]
        node_x.append(x)
        node_y.append(y)
        node_text.append(labels[uid])
        node_color.append(kind_colors.get(kinds[uid], "#000000"))
        node_hover.append(f"{uid}<br>kind: {kinds[uid]}")

    fig = go.Figure()

    # обычные рёбра
    fig.add_trace(go.Scatter(
        x=edge_x, y=edge_y,
        mode="lines",
        line=dict(width=1, color="#bbbbbb"),
        hoverinfo="none",
        showlegend=False,
    ))

    # виртуальные рёбра (пунктиром)
    if vedge_x:
        fig.add_trace(go.Scatter(
            x=vedge_x, y=vedge_y,
            mode="lines",
            line=dict(width=2, color="#e377c2", dash="dash"),
            hoverinfo="none",
            name="virtual edge (parsed from raw_code)",
        ))

    # узлы
    fig.add_trace(go.Scatter(
        x=node_x, y=node_y,
        mode="markers+text",
        marker=dict(size=22, color=node_color, line=dict(width=1, color="white")),
        text=node_text,
        textposition="bottom center",
        hovertext=node_hover,
        hoverinfo="text",
        showlegend=False,
    ))

    fig.update_layout(
        height=650,
        margin=dict(l=20, r=20, t=20, b=20),
        xaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
        yaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
        plot_bgcolor="#ffffff",
    )

    st.plotly_chart(fig, use_container_width=True)

    # -------- легенда и статистика
    lc1, lc2, lc3, lc4, lc5 = st.columns(5)
    lc1.metric("Всего узлов", len(node_ids))
    lc2.metric("Рёбер (dbt)", len(edges))
    lc3.metric("Виртуальных рёбер", len(virtual_edges))
    lc4.metric("Моделей", sum(1 for n in node_ids if kinds[n] == "model"))
    lc5.metric("Тестов", sum(1 for n in node_ids if kinds[n] == "test"))

    st.caption(
        "Сплошные серые линии — рёбра из dbt parent_map. "
        "Пунктирные розовые — рёбра, восстановленные из raw_code (ref()/dbt_schema.<name>), "
        "которых dbt не видит. "
        "Цвет узлов: серый — source, синий — model, фиолетовый — snapshot, оранжевый — test."
    )

    if virtual_edges:
        st.markdown("#### Восстановленные рёбра (virtual_edges)")
        vdf = pd.DataFrame([
            {
                "source": short_id(e["source"]),
                "target": short_id(e["target"]),
                "reason": e.get("reason", ""),
            }
            for e in virtual_edges
        ])
        st.dataframe(vdf, use_container_width=True, hide_index=True)

    # -------- полный граф (плоский список рёбер)
    with st.expander("Полный список рёбер (dbt parent_map)"):
        edf = pd.DataFrame([
            {"source": short_id(e["source"]), "target": short_id(e["target"])}
            for e in edges
        ])
        st.dataframe(edf, use_container_width=True, hide_index=True)


# ---------------------------------------------------------------- main

def main():
    st.set_page_config(
        page_title="Data Governance Platform",
        page_icon="🧭",
        layout="wide",
    )

    st.title("🧭 Data Governance Platform")
    st.caption(
        "Единый дашборд: Каталог · Контракты · Lineage. "
        "Pet-проект: собран без enterprise-решений (Collibra/Alation/Marquez)."
    )

    catalog = load_catalog()
    contract_results = load_contract_results()

    tab_catalog, tab_contracts, tab_lineage = st.tabs(
        ["📚 Каталог", "📜 Контракты", "🕸 Lineage"]
    )

    with tab_catalog:
        render_catalog_tab(catalog)

    with tab_contracts:
        render_contracts_tab(contract_results)

    with tab_lineage:
        render_lineage_tab(catalog)


if __name__ == "__main__":
    main()
