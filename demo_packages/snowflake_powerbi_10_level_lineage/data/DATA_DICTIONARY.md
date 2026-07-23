# Source Data Dictionary

All source files are synthetic, deterministic, and contain no personal data.

## order_lines.csv

| Column | Meaning |
|---|---|
| order_line_id | Unique order-line identifier |
| order_id | Business order identifier |
| order_date | Order date during 2025 |
| customer_id | Customer lookup key |
| product_id | Product lookup key |
| quantity | Ordered unit count |
| unit_price | Transaction unit price |
| discount_pct | Decimal discount rate |
| sales_channel | Online, Retail, or Partner |
| payment_mode | Card, UPI, Bank Transfer, or Wallet |
| order_status | Completed, Returned, or Cancelled |

The file includes three deliberately invalid rows. The validation levels remove them and cancelled orders, giving the presenter a visible data-quality story.

## customers.csv

Contains 80 synthetic customers with segment, region, and join date.

## products.csv

Contains 16 products across Electronics, Home Office, Outdoor, and Apparel. Each product includes list price and unit cost.

## monthly_targets.csv

Contains revenue and margin targets for each region and month. Targets are deliberately harder early in the year and achievable late in the year, producing a clear recovery narrative.

## Expected results

- `expected_story_kpis.csv` contains benchmark totals for validation.
- `expected_story_breakdown.csv` contains expected revenue, profit, and margin by region, category, and product.
- `expected_story_insights.md` contains the intended demo narrative.

Regenerate every file with:

```powershell
python tools/generate_demo_data.py
```
