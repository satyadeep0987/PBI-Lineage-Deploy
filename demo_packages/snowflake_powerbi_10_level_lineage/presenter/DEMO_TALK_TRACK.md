# Demo Talk Track

Target duration: 12-15 minutes.

## 1. Opening: the question

**Say**

"Northstar Retail had a weak start to the year but finished strongly. Leadership wants to know whether the recovery is real, what drove it, and which reports would be affected if the underlying revenue logic changed."

**Show**

The Power BI report with all filters cleared.

## 2. Executive outcome

**Say**

"The full year closes at roughly $394K revenue and 99% target attainment. That annual number hides the real story: January through April operated near 91% of target, while September through December reached about 106%."

**Show**

KPI cards and the actual-versus-target trend.

**Action**

Hover over an early month, then a late month.

## 3. Explain the recovery

**Say**

"The recovery is not only volume. We can isolate the regions, categories, segments, and channels that improved, while preserving returns as negative revenue."

**Show**

Region bar chart, category/segment chart, and product matrix.

**Action**

Select the East region, identify the strongest category, and then select the revenue-leakage segment.

## 4. Move from visual to measure

**Say**

"A dashboard result is useful only when we can prove how it was calculated. The Total Revenue measure is visually confirmed in this report, rather than inferred only because the model contains it."

**Show**

Measure Impact or Report Lineage for `Total Revenue`.

**Point out**

- Report and visual usage.
- DAX definition.
- Semantic table `Sales Story`.
- Source field `NET_SALES`.

## 5. Reveal the ten-level Snowflake path

**Say**

"The same measure connects to a physical Snowflake fact table. From there, the application walks upstream through ten levels of views and materialized tables."

**Show**

Snowflake lineage starting at:

```text
PBI_LINEAGE_DEMO.MART.FACT_PBI_SALES_STORY
```

**Narrate the chain**

1. Raw file landing.
2. Type and quality validation.
3. Valid-order materialization.
4. Customer and product enrichment.
5. Revenue, cost, and profit calculation.
6. Customer behavior logic.
7. Behavior materialization.
8. Target integration.
9. Business-story curation.
10. Power BI fact table.

**Point out**

The customer, product, and target branches make the graph interlinked and show where different business attributes enter the result.

## 6. Demonstrate impact analysis

**Say**

"Now assume the definition of net sales or the final fact table must change. Instead of manually inspecting reports, we can trace the potential impact forward."

**Show**

Table Impact for `FACT_PBI_SALES_STORY`, followed by Measure Impact for `Total Revenue`.

**Point out**

- Affected report.
- Affected semantic model.
- Related measures.
- Visual-confirmed usage versus model-level dependency.

## 7. Close with the outcome

**Say**

"The outcome is one evidence chain from a business KPI, through the report visual and DAX measure, back across ten Snowflake transformations to the original files. That shortens change assessment, improves trust, and gives report owners a concrete place to investigate."

**Finish on**

The combined lineage view or impact summary, not a setup screen.

## Questions to anticipate

**Why mix tables and views?**

Views demonstrate logical dependencies; CTAS tables demonstrate physical data movement and realistic pipeline checkpoints.

**Why use one flattened Power BI fact table?**

The report remains simple while the complexity stays visible in Snowflake lineage.

**Are the figures real customer data?**

No. Every record is synthetic and generated deterministically.

**Why can lineage take time to appear?**

Snowflake records object dependencies and data movement as metadata. Newly created relationships can require a short propagation period.

**What does visual-confirmed mean?**

The measure was found in retrieved report layout metadata. Model-level impact means the report is connected to a model containing the measure but visual usage has not yet been proven.
