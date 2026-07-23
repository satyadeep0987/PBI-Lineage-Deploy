# Start-to-Finish Runbook

## Goal

Prepare and deliver a repeatable 12-15 minute demonstration showing:

- File ingestion into Snowflake.
- Ten levels of table and view lineage.
- Business transformation logic.
- A one-page Power BI story.
- Report, measure, table, and source impact in the lineage application.

## Phase 1: Prepare Snowflake

Estimated time: 20-30 minutes.

1. Open a Snowflake worksheet.
2. Run `00_admin_setup.sql`.
3. Assign `PBI_LINEAGE_DEMO_ROLE` to the Power BI/demo user.
4. Run `01_stage_and_raw_tables.sql`.
5. Upload the four files from `data` to the internal stage.
6. Run `02_load_csv_files.sql`.
7. Verify these expected source counts:

   | Object | Expected rows |
   |---|---:|
   | `RAW_ORDER_LINES` | 1,652 |
   | `RAW_CUSTOMERS` | 80 |
   | `RAW_PRODUCTS` | 16 |
   | `RAW_MONTHLY_TARGETS` | 48 |

8. Run `03_build_10_level_lineage.sql`.
9. Run `04_comments_and_access.sql`.
10. Run `05_validate_pipeline_and_lineage.sql`.
11. Compare the KPI query with `data/expected_story_kpis.csv`.
12. Wait briefly and rerun the two `GET_LINEAGE` validation queries if Snowflake has not populated every relationship.

## Phase 2: Build Power BI

Estimated time: 25-40 minutes.

1. Import `FACT_PBI_SALES_STORY` using `PBI_LINEAGE_DEMO_ROLE`.
2. Rename the model table to `Sales Story`.
3. Add the supplied calculated columns.
4. Add the supplied measures.
5. Import `Lineage_Demo_Theme.json`.
6. Build the visuals in `REPORT_BUILD_GUIDE.md`.
7. Test every slicer and visual interaction.
8. Save as `Northstar_Sales_Lineage_Demo.pbix`.
9. Publish to the approved workspace.
10. Configure Snowflake credentials and refresh once.

## Phase 3: Prime lineage metadata

Estimated time: 10-15 minutes.

1. Open the lineage application with the approved Power BI identity.
2. Refresh workspace and report inventory.
3. Select the published report.
4. Retrieve report-definition/visual metadata before the live session.
5. Confirm these measures are visual-confirmed:

   - `Total Revenue`
   - `Gross Profit`
   - `Target Attainment %`

6. Open Snowflake lineage from `FACT_PBI_SALES_STORY`.
7. Confirm the graph reaches `RAW_ORDER_LINES` and shows customer, product, and target branches.
8. Run table impact for `FACT_PBI_SALES_STORY`.
9. Run measure impact for `Total Revenue`.
10. Keep successful screens available in browser tabs.

## Phase 4: Deliver the demo

Use `DEMO_TALK_TRACK.md`. Keep the sequence:

1. Business question.
2. Power BI outcome.
3. Measure and visual evidence.
4. Ten-level Snowflake origin.
5. Change-impact scenario.
6. Governance outcome.

This order begins with business value and then reveals the technical proof.

## Phase 5: Reset

1. Clear report slicers.
2. Return the application to its home page.
3. Suspend `PBI_LINEAGE_DEMO_WH`.
4. Retain the demo database for future sessions, or run `99_cleanup.sql` after approval.

## Time-boxed fallback

If a live service is unavailable:

- Show `powerbi/REPORT_WIREFRAME.html`.
- Show `architecture/LINEAGE_STORY.md`.
- Use `data/expected_story_insights.md` for the business result.
- Explain the validated object sequence from `architecture/lineage_map.csv`.

Do not spend more than 60 seconds troubleshooting during the presentation.
