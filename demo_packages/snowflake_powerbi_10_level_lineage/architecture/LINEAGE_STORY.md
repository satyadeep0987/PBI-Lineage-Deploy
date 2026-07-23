# Lineage Story

## Business scenario

Northstar Retail wants to explain why annual revenue recovered from a weak start and finished the year above target. The demo follows order-line data from four CSV files through ten Snowflake objects and into one Power BI page.

The final report answers three questions:

1. Are revenue and margin on target?
2. Which regions, categories, customer segments, and channels explain the result?
3. If an upstream table or measure changes, which report elements are affected?

## Primary ten-level chain

```mermaid
flowchart LR
    F[order_lines.csv] --> L1[L1 RAW_ORDER_LINES<br/>TABLE]
    L1 --> L2[L2 V_ORDER_VALIDATED<br/>VIEW]
    L2 --> L3[L3 T_ORDER_VALIDATED<br/>TABLE]
    L3 --> L4[L4 V_ORDER_ENRICHED<br/>VIEW]
    C[RAW_CUSTOMERS<br/>TABLE] --> L4
    P[RAW_PRODUCTS<br/>TABLE] --> L4
    L4 --> L5[L5 T_ORDER_FINANCIALS<br/>TABLE]
    L5 --> L6[L6 V_ORDER_BEHAVIOR<br/>VIEW]
    L6 --> L7[L7 T_ORDER_BEHAVIOR<br/>TABLE]
    L7 --> L8[L8 V_SALES_TARGET_STATUS<br/>VIEW]
    T[RAW_MONTHLY_TARGETS<br/>TABLE] --> L8
    L8 --> L9[L9 T_SALES_STORY<br/>TABLE]
    L9 --> L10[L10 FACT_PBI_SALES_STORY<br/>TABLE]
    L10 --> PBI[Power BI semantic model]
    PBI --> R[One-page executive report]
```

## Why tables and views are alternated

- Views make transformations and object dependencies visible.
- CTAS tables demonstrate materialized data movement.
- Customer, product, and target branches make the lineage graph interlinked.
- The final object is a physical fact table, which gives Power BI a simple and stable source.

## Demonstration moments

- Start at `FACT_PBI_SALES_STORY` and trace upstream through all ten objects.
- Start at `RAW_ORDER_LINES` and trace downstream to demonstrate impact analysis.
- Select `NET_SALES` or `GROSS_PROFIT` to explain column and measure lineage.
- In Power BI, select a visual using `[Total Revenue]`, then connect the measure back to Snowflake.

Snowflake records both view dependencies and data movement created by CTAS. Account lineage requires Enterprise Edition or higher and appropriate lineage/object privileges.
