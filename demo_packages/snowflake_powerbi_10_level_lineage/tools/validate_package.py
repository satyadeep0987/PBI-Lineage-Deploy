"""Validate the portable demo package without requiring Snowflake or Power BI."""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_csv(relative_path: str) -> list[dict[str, str]]:
    with (ROOT / relative_path).open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> None:
    required_files = [
        "README.md",
        "architecture/LINEAGE_STORY.md",
        "architecture/lineage_map.csv",
        "data/order_lines.csv",
        "data/customers.csv",
        "data/products.csv",
        "data/monthly_targets.csv",
        "data/expected_story_kpis.csv",
        "data/expected_story_breakdown.csv",
        "snowflake/00_admin_setup.sql",
        "snowflake/01_stage_and_raw_tables.sql",
        "snowflake/02_load_csv_files.sql",
        "snowflake/03_build_10_level_lineage.sql",
        "snowflake/04_comments_and_access.sql",
        "snowflake/05_validate_pipeline_and_lineage.sql",
        "snowflake/06_demo_story_queries.sql",
        "snowflake/99_cleanup.sql",
        "powerbi/PowerQuery_FactSalesStory.m",
        "powerbi/DAX_Measures.dax",
        "powerbi/DAX_Calculated_Columns.dax",
        "powerbi/Lineage_Demo_Theme.json",
        "powerbi/REPORT_BUILD_GUIDE.md",
        "powerbi/REPORT_WIREFRAME.html",
        "powerbi/REPORT_WIREFRAME.png",
        "presenter/START_TO_FINISH_RUNBOOK.md",
        "presenter/DEMO_TALK_TRACK.md",
        "presenter/PRE_DEMO_CHECKLIST.md",
        "integration/app_settings_reference.json",
    ]
    for relative_path in required_files:
        require((ROOT / relative_path).is_file(), f"Missing required file: {relative_path}")

    customers = read_csv("data/customers.csv")
    products = read_csv("data/products.csv")
    orders = read_csv("data/order_lines.csv")
    targets = read_csv("data/monthly_targets.csv")

    require(len(customers) == 80, "customers.csv must contain 80 rows")
    require(len(products) == 16, "products.csv must contain 16 rows")
    require(len(orders) == 1652, "order_lines.csv must contain 1,652 rows")
    require(len(targets) == 48, "monthly_targets.csv must contain 48 rows")
    require(
        len({row["order_line_id"] for row in orders}) == len(orders),
        "order_line_id values must be unique",
    )

    lineage_rows = read_csv("architecture/lineage_map.csv")
    lineage_levels = [int(row["level"]) for row in lineage_rows]
    require(lineage_levels == list(range(1, 11)), "Lineage map must contain levels 1 through 10")

    build_sql = (ROOT / "snowflake/03_build_10_level_lineage.sql").read_text(encoding="utf-8")
    for row in lineage_rows[1:]:
        object_tail = row["object_name"].split(".", 1)[1]
        require(object_tail in build_sql, f"Build SQL does not create {object_tail}")

    created_objects = re.findall(
        r"CREATE\s+OR\s+REPLACE\s+(?:TABLE|VIEW)\s+([A-Z0-9_.]+)",
        build_sql,
        flags=re.IGNORECASE,
    )
    require(len(created_objects) == 9, "Build SQL must create levels 2 through 10")

    power_query = (ROOT / "powerbi/PowerQuery_FactSalesStory.m").read_text(encoding="utf-8")
    require(
        "FACT_PBI_SALES_STORY" in power_query,
        "Power Query must connect to the final Snowflake fact table",
    )

    dax_measures = (ROOT / "powerbi/DAX_Measures.dax").read_text(encoding="utf-8")
    for measure in ("Total Revenue", "Gross Profit", "Target Attainment %"):
        require(
            re.search(rf"(?m)^{re.escape(measure)}\s*=", dax_measures) is not None,
            f"Missing required measure: {measure}",
        )

    for json_file in (
        "powerbi/Lineage_Demo_Theme.json",
        "integration/app_settings_reference.json",
    ):
        with (ROOT / json_file).open(encoding="utf-8") as handle:
            json.load(handle)

    app_reference = json.loads(
        (ROOT / "integration/app_settings_reference.json").read_text(encoding="utf-8")
    )
    settings = app_reference["snowflake_lineage"]
    require(settings["database"] == "PBI_LINEAGE_DEMO", "Reference database name is inconsistent")
    require(settings["max_depth"] >= 12, "Application reference depth must cover ten levels")
    require("SET_ONLY" in settings["password"], "Reference configuration must not contain a password")

    print("PASS: demo package structure")
    print("PASS: deterministic CSV counts and unique order-line grain")
    print("PASS: ten-level map and Snowflake build-object coverage")
    print("PASS: Power BI source, required measures, and JSON files")
    print("PASS: reference settings contain placeholders only")


if __name__ == "__main__":
    main()
