# Snowflake to Power BI Ten-Level Lineage Demo

This isolated package creates a synthetic retail story with:

- Four CSV source files.
- A primary ten-level Snowflake lineage chain.
- Interlinked customer, product, and target branches.
- Alternating tables and views.
- A final physical fact table for Power BI.
- DAX measures and calculated columns.
- A one-page report design.
- Presenter notes from setup through delivery.

Nothing in this package changes the existing PBI Lineage Explorer application.

## Demo story

Northstar Retail began 2025 below revenue target, improved during the middle of the year, and finished the final four months above target. The report explains which regions, products, customer segments, and channels drove the recovery, while preserving returned orders as negative revenue.

Expected headline results:

| Metric | Expected result |
|---|---:|
| Revenue | About $393.9K |
| Gross profit | About $215.8K |
| Gross margin | About 54.8% |
| Orders | 939 |
| Customers | 80 |
| Full-year target attainment | About 99.4% |
| January-April attainment | About 90.9% |
| September-December attainment | About 105.9% |

## Folder map

| Folder | Contents |
|---|---|
| `data` | Synthetic CSV files, expected KPIs, and data dictionary |
| `snowflake` | Setup, load, ten-level transformation, validation, demo-query, and cleanup SQL |
| `powerbi` | Power Query, DAX, theme, report guide, and visual wireframe |
| `architecture` | Lineage diagram and machine-readable level map |
| `integration` | Optional reference settings for the existing lineage application |
| `presenter` | Complete runbook, talk track, and checklist |
| `tools` | Deterministic data generator |

## Prerequisites

- Snowflake access capable of creating an isolated database and X-Small warehouse.
- Snowflake Enterprise Edition or higher for native lineage.
- A role with `VIEW LINEAGE` and access to the demo objects.
- Snowsight stage upload or SnowSQL for the four CSV files.
- Power BI Desktop.
- A Power BI workspace where the presenter can publish and retrieve report definitions.
- A configured Windows XMLA environment only if XMLA semantic-model lineage will be shown.

## Build order

1. Regenerate data only when required:

   ```powershell
   python tools\generate_demo_data.py
   ```

2. Run `snowflake/00_admin_setup.sql`.
3. Run `snowflake/01_stage_and_raw_tables.sql`.
4. Upload these files to `@PBI_LINEAGE_DEMO.RAW.DEMO_FILES`:

   - `data/order_lines.csv`
   - `data/customers.csv`
   - `data/products.csv`
   - `data/monthly_targets.csv`

5. Run `snowflake/02_load_csv_files.sql`.
6. Run `snowflake/03_build_10_level_lineage.sql`.
7. Run `snowflake/04_comments_and_access.sql`.
8. Run `snowflake/05_validate_pipeline_and_lineage.sql`.
9. Build the Power BI page using `powerbi/REPORT_BUILD_GUIDE.md`.
10. Publish and refresh the report.
11. Use `presenter/PRE_DEMO_CHECKLIST.md`.
12. Deliver the story with `presenter/DEMO_TALK_TRACK.md`.

## Primary Snowflake chain

| Level | Object | Type |
|---:|---|---|
| 1 | `RAW.RAW_ORDER_LINES` | Table |
| 2 | `STAGE.V_ORDER_VALIDATED` | View |
| 3 | `STAGE.T_ORDER_VALIDATED` | Table |
| 4 | `CORE.V_ORDER_ENRICHED` | View |
| 5 | `CORE.T_ORDER_FINANCIALS` | Table |
| 6 | `CORE.V_ORDER_BEHAVIOR` | View |
| 7 | `ANALYTICS.T_ORDER_BEHAVIOR` | Table |
| 8 | `ANALYTICS.V_SALES_TARGET_STATUS` | View |
| 9 | `MART.T_SALES_STORY` | Table |
| 10 | `MART.FACT_PBI_SALES_STORY` | Table |

The final table deliberately avoids an additional presentation view, so the lineage application receives an unambiguous `TABLE` endpoint.

## Refresh process

For a complete data refresh:

1. Replace the four files on the Snowflake stage.
2. Run `02_load_csv_files.sql`.
3. Run `03_build_10_level_lineage.sql`.
4. Run `04_comments_and_access.sql`.
5. Refresh the Power BI semantic model.
6. Refresh report-definition metadata in the lineage application.

Re-running the transformation script recreates the materialized tables and records fresh CTAS lineage.

## Important notes

- Snowflake `GET_LINEAGE` accepts a maximum distance of five in one call. The validation script uses two segments; the application recursively requests one hop and can display the complete chain.
- Native Snowflake lineage can take a short time to appear after CTAS and view creation.
- Keep `FACT_PBI_SALES_STORY` as the Power BI source. Connecting Power BI to a CSV fallback would remove the Snowflake handoff from the demonstration.
- Run `snowflake/99_cleanup.sql` only after the demo environment is no longer needed.

## Useful references

- [Snowflake data lineage](https://docs.snowflake.com/en/user-guide/ui-snowsight-lineage)
- [Snowflake GET_LINEAGE](https://docs.snowflake.com/en/sql-reference/functions/get_lineage-snowflake-core)
- [Microsoft Power Query Snowflake connector](https://learn.microsoft.com/en-us/power-query/connectors/snowflake)
- [Power BI calculated columns](https://learn.microsoft.com/en-us/power-bi/transform-model/desktop-calculated-columns)
