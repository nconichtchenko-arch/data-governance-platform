#!/usr/bin/env python3
"""
contract_validator.py
=====================
Проверяет, соответствуют ли реальные данные в PostgreSQL
YAML-контракту (contracts/*.yml).

Что проверяем:
  1. Таблица существует и доступна
  2. Каждая колонка из контракта есть в таблице (по имени)
  3. Тип колонки соответствует ожидаемому (по семействам: text/int/numeric/date/timestamp/bool)
  4. NOT NULL: колонки с nullable=false не содержат NULL
  5. primary_key: комбинация уникальна (нет дублей)
  6. constraints: правило (шаблон с {col}) разворачивается в
     "SELECT count(*) FROM ... WHERE col IS NOT NULL AND NOT (<rule>)"
     Если count > 0 — нарушение.
  7. freshness: если включено — max(order_date) не старше max_age_hours

Изоляция ошибок: каждое исключение в run_check делает conn.rollback(),
поэтому одна битая проверка не прерывает следующие.

Выход:
  - data/contract_results.json  (машинночитаемый отчёт)
  - печать в консоль

Возвращает exit-code 0 если все проверки прошли, 1 если есть FAIL/ERROR.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONTRACTS_DIR = PROJECT_ROOT / "contracts"
RESULTS_PATH = PROJECT_ROOT / "data" / "contract_results.json"

DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "dbname": "dwh_marketplace",
    "user": "dwh_user",
    "password": "dwh_password",
}

# Семейства типов PostgreSQL: сводим разные написания к одному "семейству".
# Это нужно, чтобы "text" в контракте матчился с "character varying" из information_schema.
TYPE_FAMILIES = {
    # text
    "text": "text",
    "character varying": "text",
    "varchar": "text",
    "character": "text",
    "char": "text",
    # integer
    "integer": "integer",
    "int": "integer",
    "int4": "integer",
    "bigint": "integer",
    "int8": "integer",
    "smallint": "integer",
    "int2": "integer",
    # numeric
    "numeric": "numeric",
    "decimal": "numeric",
    "real": "numeric",
    "double precision": "numeric",
    "float4": "numeric",
    "float8": "numeric",
    # date / timestamp
    "date": "date",
    "timestamp without time zone": "timestamp",
    "timestamp with time zone": "timestamp",
    "timestamptz": "timestamp",
    "timestamp": "timestamp",
    # bool
    "boolean": "boolean",
    "bool": "boolean",
}


def type_family(t: str) -> str:
    if not t:
        return "unknown"
    return TYPE_FAMILIES.get(t.lower().strip(), t.lower().strip())


def load_contract(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_columns(cur, schema: str, table: str) -> dict:
    cur.execute(
        """
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        ORDER BY ordinal_position
        """,
        (schema, table),
    )
    return {row[0]: row[1] for row in cur.fetchall()}


def run_check(conn, name: str, sql: str, params=None) -> dict:
    cur = conn.cursor()
    try:
        cur.execute(sql, params or ())
        row = cur.fetchone()
        violations = int(row[0]) if row and row[0] is not None else 0
        return {
            "name": name,
            "status": "PASS" if violations == 0 else "FAIL",
            "violations": violations,
            "sql": sql.strip(),
        }
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        return {
            "name": name,
            "status": "ERROR",
            "violations": None,
            "error": str(e),
            "sql": sql.strip(),
        }
    finally:
        cur.close()


def expand_rule(rule: str, col_name: str) -> str:
    quoted = f'"{col_name}"'
    return rule.replace("{col}", quoted)


def validate_contract(contract_doc: dict, conn) -> dict:
    c = contract_doc["contract"]
    schema = c["schema"]
    table = c["table"]
    fq = f'"{schema}"."{table}"'

    checks = []
    cur = conn.cursor()

    # 0. Таблица существует?
    cur.execute(
        """
        SELECT count(*) FROM information_schema.tables
        WHERE table_schema = %s AND table_name = %s
        """,
        (schema, table),
    )
    table_exists = cur.fetchone()[0] == 1
    checks.append({
        "name": "table_exists",
        "status": "PASS" if table_exists else "FAIL",
        "details": f"{schema}.{table}",
    })
    cur.close()

    if not table_exists:
        return {
            "dataset": c["dataset"],
            "schema": schema,
            "table": table,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "checks": checks,
            "overall": "FAIL",
        }

    cur = conn.cursor()
    actual_cols = get_columns(cur, schema, table)
    cur.close()

    # 1. Колонки присутствуют И тип соответствует
    for col_name, col_spec in (c.get("columns") or {}).items():
        present = col_name in actual_cols
        expected_type = col_spec.get("type")
        actual_type = actual_cols.get(col_name)

        # presence
        checks.append({
            "name": f"column_present::{col_name}",
            "status": "PASS" if present else "FAIL",
            "details": f"actual={actual_type or 'MISSING'}",
        })
        if not present:
            continue

        # type check
        expected_family = type_family(expected_type)
        actual_family = type_family(actual_type)
        type_ok = (expected_family == actual_family) or (expected_family == "unknown")
        checks.append({
            "name": f"column_type::{col_name}",
            "status": "PASS" if type_ok else "FAIL",
            "details": f"expected={expected_type} ({expected_family}), actual={actual_type} ({actual_family})",
        })

    # 2. NOT NULL
    for col_name, col_spec in (c.get("columns") or {}).items():
        if col_spec.get("nullable") is False and col_name in actual_cols:
            sql = f'SELECT count(*) FROM {fq} WHERE "{col_name}" IS NULL'
            checks.append(run_check(conn, f"not_null::{col_name}", sql))

    # 3. Primary key — уникальность
    pk = c.get("primary_key") or []
    if pk:
        cols_csv = ", ".join(f'"{col}"' for col in pk)
        sql = f"""
            SELECT count(*) FROM (
                SELECT {cols_csv}, count(*) AS c
                FROM {fq}
                GROUP BY {cols_csv}
                HAVING count(*) > 1
            ) dup
        """
        checks.append(run_check(conn, f"primary_key_unique::({','.join(pk)})", sql))

    # 4. Constraints (шаблон с {col})
    for col_name, col_spec in (c.get("columns") or {}).items():
        for constraint in (col_spec.get("constraints") or []):
            rule = constraint.get("rule")
            cname = constraint.get("name", "constraint")
            if not rule or col_name not in actual_cols:
                continue
            rule_expanded = expand_rule(rule, col_name)
            sql = (
                f'SELECT count(*) FROM {fq} '
                f'WHERE "{col_name}" IS NOT NULL AND NOT ({rule_expanded})'
            )
            checks.append(
                run_check(conn, f"constraint::{col_name}::{cname}", sql)
            )

    # 5. Freshness (опционально)
    freshness = c.get("freshness") or {}
    if freshness.get("enabled") and "order_date" in actual_cols:
        max_age = int(freshness.get("max_age_hours", 24))
        sql = f"""
            SELECT count(*) FROM (
                SELECT max("order_date")::timestamp AS mx FROM {fq}
            ) t
            WHERE mx < now() - interval '{max_age} hours'
        """
        checks.append(run_check(conn, f"freshness::max_age_{max_age}h", sql))

    overall = "PASS" if all(c["status"] == "PASS" for c in checks) else "FAIL"
    return {
        "dataset": c["dataset"],
        "schema": schema,
        "table": table,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "checks": checks,
        "overall": overall,
    }


def print_report(report: dict) -> None:
    print(f"\n=== Contract: {report['dataset']} ({report['schema']}.{report['table']}) ===")
    print(f"Overall: {report['overall']}")
    print(f"Checked: {report['checked_at']}\n")
    for chk in report["checks"]:
        status = chk["status"]
        marker = "[OK]  " if status == "PASS" else "[FAIL]" if status == "FAIL" else "[ERR] "
        print(f"{marker} {chk['name']:55s} {status}")
        if status != "PASS":
            detail = chk.get("details") or chk.get("error")
            if detail is None:
                detail = f"violations={chk.get('violations')}"
            print(f"       details: {detail}")
    print()


def main():
    if not CONTRACTS_DIR.exists():
        print(f"[!] Нет папки с контрактами: {CONTRACTS_DIR}")
        sys.exit(1)

    yaml_files = sorted(CONTRACTS_DIR.glob("*.yml")) + sorted(CONTRACTS_DIR.glob("*.yaml"))
    if not yaml_files:
        print(f"[!] Нет YAML-контрактов в {CONTRACTS_DIR}")
        sys.exit(1)

    try:
        conn = psycopg2.connect(**DB_CONFIG)
        conn.autocommit = True
    except Exception as e:
        print(f"[!] Не удалось подключиться к PostgreSQL: {e}")
        sys.exit(1)

    all_reports = []
    exit_code = 0
    try:
        for yml in yaml_files:
            doc = load_contract(yml)
            report = validate_contract(doc, conn)
            all_reports.append(report)
            print_report(report)
            if report["overall"] != "PASS":
                exit_code = 1
    finally:
        conn.close()

    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "contracts_count": len(all_reports),
        "overall": "PASS" if exit_code == 0 else "FAIL",
        "reports": all_reports,
    }
    with RESULTS_PATH.open("w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(f"[OK] Отчёт записан: {RESULTS_PATH}")
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
