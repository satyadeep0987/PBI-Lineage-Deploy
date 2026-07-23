# Pre-Demo Checklist

## One day before

- [ ] Snowflake database and warehouse exist.
- [ ] All four source files are present on the internal stage.
- [ ] Every SQL script through `05_validate_pipeline_and_lineage.sql` succeeds.
- [ ] The final fact table contains expected rows and KPIs.
- [ ] Snowflake lineage reaches all ten primary levels.
- [ ] `PBI_LINEAGE_DEMO_ROLE` can query the final table.
- [ ] Power BI report is published and refreshed.
- [ ] Report-definition metadata has been retrieved.
- [ ] Total Revenue is visually confirmed.
- [ ] Table Impact and Measure Impact both return the demo report.

## Thirty minutes before

- [ ] Start or resume the required local Windows/XMLA service.
- [ ] Open the lineage application and confirm the session is authenticated.
- [ ] Open the Power BI report with all filters cleared.
- [ ] Open Snowflake Snowsight on the final fact table lineage page.
- [ ] Run the KPI validation query.
- [ ] Close unrelated windows and hide credentials.
- [ ] Set browser zoom and display scaling for the presentation screen.
- [ ] Confirm internet, VPN, and corporate proxy connectivity.
- [ ] Keep `REPORT_WIREFRAME.html` and `LINEAGE_STORY.md` open as fallback.

## Immediately before speaking

- [ ] Suspend notifications.
- [ ] Confirm the correct Power BI workspace and Snowflake role.
- [ ] Clear application filters and stale errors.
- [ ] Keep the final impact-summary screen one click away.
- [ ] Start a timer for 12-15 minutes.

## Do not expose

- Snowflake passwords or connection strings.
- Power BI access or refresh tokens.
- Streamlit secrets.
- Tenant IDs or application secrets unless the audience is explicitly authorized.
- Browser tabs containing administration pages.

## Recovery rules

- If Snowflake lineage is delayed, use the validated two-segment `GET_LINEAGE` output.
- If report-definition retrieval fails, use the previously retrieved visual metadata.
- If XMLA fails, continue with REST/Fabric report lineage and the Snowflake lineage graph.
- If Power BI refresh fails, use the already imported model.
- If a failure consumes 60 seconds, switch to the prepared fallback and continue the story.
