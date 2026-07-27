# User Experience, KPI Demo, and Functional Coverage

## Purpose

This document turns the eight requested scenarios into a practical user experience and demo flow for PBI Lineage Explorer. It distinguishes confirmed metadata from unavailable metadata so the app never presents assumptions as lineage facts.

## Functional Coverage

| Requested scenario | User-facing behavior | Status and conditions |
| --- | --- | --- |
| Show multiple semantic tables | After selecting a report, **Semantic Model Objects** lists every accessible table, column, calculated column, and measure from its semantic model. PowerAI can search the authorized estate across reports and semantic models. | Available; full semantic metadata requires XMLA access. |
| Tabular Editor-style model detail and Snowflake lineage | **Relationships** now shows table-to-table / column-to-column model relationships, active state, and returned filtering/cardinality properties. **Measure Source Lineage** shows DAX dependencies and has a multi-select Snowflake column-lineage handoff for measures whose physical source mapping is available. | Relationships require XMLA. Snowflake tracing requires enabled Snowflake lineage settings and a mapped source object/column. |
| Incorrect or incomplete measure name | **Measure Impact** now returns close, real measure names from the scanned authorized models when no exact match is found. It also retains the existing optional partial-name match mode. | Available. Suggestions are metadata matches, never generated guesses. |
| Layman-language measure definition | In Report Lineage, users can select measures and obtain **Definition**, **Business meaning**, **DAX logic**, and **Source lineage**. PowerAI instructions now also require a short **Plain-English definition** whenever a measure is discussed. | Available when PowerAI or Snowflake Cortex measure-definition provider is enabled; otherwise the app clearly reports that the provider is unavailable. |
| Download full report details | Report Lineage now has **Download full report details**, which creates one ZIP file. | Available. It is a metadata package, not a PBIX export or a copy of report data. |
| Ask which reports use a Snowflake field and show DAX/visuals | PowerAI can search the estate, inspect matching measure lineage, and include visual evidence when the question explicitly asks for visuals. Measure/table impact output now includes confirmed visual name, page, and type together with DAX and source fields. | Visual claims require a retrieved/uploaded report definition. Snowflake graph tracing additionally requires Snowflake lineage configuration. |
| Ask PowerAI for a lineage diagram | When a question explicitly asks for a table or column/measure-source lineage diagram, PowerAI routes the request to the read-only Snowflake trace tool and renders the returned table or column lineage as an interactive diagram directly below the chat answer. | The diagram is available only when the model can identify a mapped Snowflake source and the Snowflake trace returns rows. |
| Make the experience simple | The intended primary path is report-first and KPI-first, with PowerAI as an assistant—not a prerequisite. | Described in the flow below. |
| Select report, then a KPI/measure, and understand it end-to-end | A report user can move from measure definition to DAX, semantic dependencies, source fields, Snowflake lineage, and visual use. | Available through the guided KPI walkthrough below. |

## Recommended KPI Walkthrough

1. Open **Report Lineage** and select a report.
2. In **Semantic Model Objects**, find the KPI measure and its home table. Use **Relationships** when the KPI depends on dimensions or date tables.
3. Open **Measure Source Lineage** and filter/select the KPI measure. Review its DAX expression, dependent semantic objects, source table/view, and source column.
4. Choose **Get detailed measure definitions** to receive a business-language explanation beside the technical DAX and lineage.
5. Select the mapped Snowflake source column and choose **Get Snowflake column lineage** to trace upstream/downstream Snowflake objects. The app only enables a trace for source information actually returned by the semantic metadata.
6. Open **Visual Details** to retrieve or upload the report definition, then open **Visual Item Lineage** to verify the KPI's page, visual, role, semantic object, and source mapping.
7. Use **Download full report details** to hand off the full metadata package.

This gives a business user one mental model: **report → KPI → DAX → model dependencies → source → Snowflake → visual**.

## PowerAI Example: Snowflake Field to Reports, DAX, and Visuals

Example question:

> `How many reports use NETSALESAMT from Snowflake view DEMO_ANALYTICS.CORE.SALES_VIEW? Show the measures, DAX, and confirmed visuals in each report.`

Expected answer structure:

1. **Answer / scope** — number of affected reports in the selected workspaces, including any model-level versus visual-level caveat.
2. **Reports using the source** — report name, workspace, semantic model, and matching measure(s).
3. **Measure logic** — measure name, home table, DAX expression, dependency/source field, and a plain-English definition.
4. **Confirmed visual use** — page name, visual name, visual type, and field role for every report-definition match.
5. **Snowflake lineage** — configured upstream/downstream Snowflake trace, when available.
6. **Evidence gaps** — reports where report-definition metadata or Snowflake metadata could not be retrieved.

The answer must not claim that a measure is used by a visual merely because it exists in that report's semantic model. The app labels a report as model-connected until the report definition confirms a visual match.

## PowerAI Table and Measure-Column Diagrams

PowerAI now renders an interactive diagram when the user asks for a diagram/graph, including ordinary wording such as “show this measure lineage with a diagram.”

- **Table/view request:** PowerAI traces the returned Snowflake table/view and renders the table lineage graph.
- **Measure/column request:** PowerAI first identifies the measure's mapped source object and column from the authorized Power BI metadata, then traces that Snowflake column and renders the column lineage graph, including returned transformation details.

Example prompts:

> `Show the upstream lineage diagram for DEMO_ANALYTICS.CORE.SALES_VIEW.`

> `Show the Net Sales measure lineage with a diagram, including its Snowflake source column.`

The diagram is not generated from model prose: it appears only when the read-only Snowflake lineage tool returns trace rows. If the semantic model has no mapped physical column, Snowflake tracing is disabled, or the trace returns no rows, PowerAI explains that evidence gap instead of drawing a speculative diagram.

## What the Report Details ZIP Looks Like

The download is named like `Executive_Sales_lineage_details.zip` and contains:

| File | Contents |
| --- | --- |
| `README.md` | Human-readable report context, row counts, data-availability notes, and a file guide. |
| `report_context.json` | Workspace, report, report ID, dataset ID, type, and format. |
| `semantic_model_objects.csv` | Semantic tables, columns, calculated columns, measures, data types, and returned DAX. |
| `semantic_relationships.csv` | From/to tables and columns, relationship active state, and returned filtering/cardinality values. |
| `source_database_lineage.csv` | Source server/database/schema/object and available native-query metadata. |
| `measure_source_lineage.csv` | Measure DAX dependencies and mapped semantic/source fields. |
| `visual_usage.csv` | Included only after a report definition is retrieved or uploaded; lists page, visual, type, role, and referenced fields. |

The package intentionally excludes report data rows, access tokens, credentials, and a PBIX. A PBIX download remains subject to Power BI tenant/report policy and is separate from this metadata export.

## Demo Flow

Use a single relatable KPI—such as Net Sales—as the demo thread.

1. **Start simple (30 seconds).** Select the report. State that the user did not need to know XMLA, DAX syntax, or Snowflake object names to begin.
2. **Explain the KPI (60 seconds).** Select Net Sales in Measure Source Lineage. Show its plain-English definition and DAX logic.
3. **Show semantic context (30 seconds).** Open Relationships and explain how the fact table joins to Date/Product/Customer dimensions.
4. **Prove the source (45 seconds).** Show the mapped Snowflake view and column, then launch Snowflake lineage.
5. **Prove report use (45 seconds).** Retrieve Visual Details and show the KPI's page/card/chart through Visual Item Lineage.
6. **Show an imperfect request (30 seconds).** Search a misspelled measure name such as `NetSaleAmt`; show the suggested actual measure names instead of a dead end.
7. **Finish with the handoff (30 seconds).** Download the report-details ZIP and explain that it is auditable metadata for analysts, owners, and change-review teams.
8. **Optionally use PowerAI.** Ask the example question above to demonstrate conversational discovery; show the trace/evidence and explain that it uses the same read-only app data.

## Preconditions and Honest Limitations

- Semantic objects, DAX dependencies, and relationships require a compatible Power BI XMLA endpoint, MSOLAP, and permissions on a Windows-capable host.
- Visual-level results require a Fabric report definition or a manually uploaded report layout. Without it, the app can report model-level impact but not a confirmed visual.
- Snowflake lineage is available only for physical sources successfully mapped from the semantic model and when the configured Snowflake metadata procedure is accessible.
- A relationship field marked `Not returned` means that the capacity's XMLA DMV did not expose that property; it does not mean the relationship has that value.
- Measure suggestions use only the currently authorized/scanned model metadata. They do not search data values or infer new measures.
- PowerAI is read-only and must base its statements on returned metadata. It should name missing evidence rather than guess.
