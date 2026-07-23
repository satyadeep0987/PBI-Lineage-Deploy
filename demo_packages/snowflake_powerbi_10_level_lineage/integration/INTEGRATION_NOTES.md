# Existing Application Integration

The files in this folder are references only. They do not modify the application configuration.

## Snowflake lineage

Use the values in `app_settings_reference.json` when the demo connection is approved. Supply the real user and password through the deployment's existing secret mechanism, not source control.

The recommended lineage starting object is:

```text
PBI_LINEAGE_DEMO.MART.FACT_PBI_SALES_STORY
```

The final source is a table, so the object domain should resolve to `TABLE`. Set recursive depth to at least 12; the reference uses 20.

## Power BI lineage

The Power Query navigation path should remain:

```text
Snowflake -> PBI_LINEAGE_DEMO -> MART -> FACT_PBI_SALES_STORY
```

Do not replace this with a local CSV source in the published report. The lineage application needs the Snowflake navigation metadata to connect semantic objects to the final fact table.

## Recommended demo selections

- Report: `Northstar_Sales_Lineage_Demo`
- Semantic table: `Sales Story`
- Measure: `Total Revenue`
- Source field: `NET_SALES`
- Table-impact input: `FACT_PBI_SALES_STORY`
- Snowflake starting object: `PBI_LINEAGE_DEMO.MART.FACT_PBI_SALES_STORY`

## Credential precaution

Use a dedicated demo identity with `PBI_LINEAGE_DEMO_ROLE`. Do not place credentials in this folder or commit a populated configuration file.
