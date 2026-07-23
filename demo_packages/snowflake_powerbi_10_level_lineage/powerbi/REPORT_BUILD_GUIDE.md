# One-Page Power BI Report

## Report objective

Build a one-page executive report named **From Orders to Customer Value**. The visual sequence should tell the story from annual performance, to the monthly recovery, to the business drivers and leakage.

Use **Import** mode for the demo. It gives predictable performance, supports the supplied calculated columns, and creates a straightforward semantic model for XMLA inspection.

## Connect to Snowflake

1. Open Power BI Desktop.
2. Select **Get data > Snowflake**.
3. Enter the Snowflake server hostname and `PBI_LINEAGE_DEMO_WH`.
4. Open Advanced options and enter `PBI_LINEAGE_DEMO_ROLE`.
5. Choose **Import**.
6. Select `PBI_LINEAGE_DEMO > MART > FACT_PBI_SALES_STORY`.
7. Select **Transform data**.
8. Rename the query to `Sales Story`.
9. Confirm the date, whole-number, currency, decimal, and percentage types shown in `PowerQuery_FactSalesStory.m`.
10. Select **Close & Apply**.

Alternatively, create a blank query, open Advanced Editor, paste `PowerQuery_FactSalesStory.m`, and replace the Snowflake account hostname.

## Add semantic logic

1. Add every expression in `DAX_Calculated_Columns.dax` as a calculated column.
2. Add every expression in `DAX_Measures.dax` as a measure.
3. Format revenue, target, profit, variance, and order value measures as currency.
4. Format margin, attainment, and return rate measures as percentages with one decimal.
5. Sort `MONTH_LABEL` by `Month Sort`, or use `Order Month` on the trend axis.
6. Place measures in a display folder named `Demo Measures`.
7. Hide technical fields such as `SOURCE_FILE`, `INGESTED_AT`, and `MART_REFRESHED_AT` from report view.

## Page layout

Set the page to 16:9 and use the supplied JSON theme.

### Header band

- Title: **From Orders to Customer Value**
- Subtitle: **How customer, product, and channel decisions moved revenue from below target to a strong finish**
- Slicers: `Order Month`, `REGION`, `CUSTOMER_SEGMENT`, and `SALES_CHANNEL`

### KPI row

Create five card visuals:

| Card | Measure |
|---|---|
| Revenue | `[Total Revenue]` |
| Gross Profit | `[Gross Profit]` |
| Gross Margin | `[Gross Margin %]` |
| Orders | `[Orders]` |
| Target Attainment | `[Target Attainment %]` |

Apply conditional font color to Target Attainment using `[KPI Status Color]`.

### Main story row

**Actual versus target**

- Visual: Line and clustered column chart
- X-axis: `Order Month`
- Column: `[Total Revenue]`
- Line: `[Revenue Target]`
- Tooltip: `[Revenue Variance]`, `[Gross Margin %]`, and `[Orders]`

**Revenue by region**

- Visual: Clustered bar chart
- Y-axis: `REGION`
- X-axis: `[Total Revenue]` and `[Revenue Target]`
- Tooltip: `[Gross Profit]`, `[Gross Margin %]`, and `[Return Rate %]`

### Driver row

**Category and segment drivers**

- Visual: Stacked column chart
- X-axis: `CATEGORY`
- Legend: `CUSTOMER_SEGMENT`
- Y-axis: `[Total Revenue]`
- Tooltip: `[Gross Profit]`, `[Orders]`, and `[Average Order Value]`

**Product contribution**

- Visual: Matrix
- Rows: `CATEGORY`, `PRODUCT_NAME`
- Values: `[Total Revenue]`, `[Gross Profit]`, `[Gross Margin %]`, `[Returned Revenue]`
- Apply data bars to Total Revenue.

**Revenue leakage**

- Visual: Donut chart
- Legend: `STORY_SIGNAL`
- Values: `[Total Revenue]`
- Add `[Return Rate %]` as a small card immediately above it.

## Interaction check

1. Select a month in the trend and confirm every driver visual filters.
2. Select the East region and identify its strongest category.
3. Select `REVENUE LEAKAGE` and show the returned orders contributing negative revenue.
4. Reset all filters before saving.

## Publish and verify

1. Save as `Northstar_Sales_Lineage_Demo.pbix`.
2. Publish to the selected Power BI workspace.
3. Open the semantic model settings and configure Snowflake credentials.
4. Refresh once in the Power BI service.
5. Open the lineage application and retrieve report definition/visual metadata.
6. Confirm `[Total Revenue]`, `[Gross Profit]`, and `[Target Attainment %]` appear as visual-confirmed measures.
7. Start Snowflake lineage from `PBI_LINEAGE_DEMO.MART.FACT_PBI_SALES_STORY`.
