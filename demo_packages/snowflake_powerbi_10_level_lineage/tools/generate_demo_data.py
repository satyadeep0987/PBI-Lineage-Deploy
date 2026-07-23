"""Generate deterministic source files for the Snowflake and Power BI lineage demo."""

from __future__ import annotations

import csv
import random
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
SEED = 20260723


PRODUCTS = [
    ("P001", "Pulse Wireless Earbuds", "Electronics", "Audio", 42.00, 89.00),
    ("P002", "Orbit Smart Watch", "Electronics", "Wearables", 96.00, 189.00),
    ("P003", "Nova Portable Monitor", "Electronics", "Displays", 118.00, 239.00),
    ("P004", "Arc Mechanical Keyboard", "Electronics", "Accessories", 48.00, 109.00),
    ("P005", "Focus Ergonomic Chair", "Home Office", "Furniture", 142.00, 329.00),
    ("P006", "Rise Standing Desk", "Home Office", "Furniture", 188.00, 449.00),
    ("P007", "Beam Desk Lamp", "Home Office", "Lighting", 24.00, 69.00),
    ("P008", "Slate Desk Organizer", "Home Office", "Accessories", 12.00, 39.00),
    ("P009", "Trail Rain Jacket", "Outdoor", "Apparel", 38.00, 99.00),
    ("P010", "Summit Day Pack", "Outdoor", "Bags", 31.00, 84.00),
    ("P011", "Terra Insulated Bottle", "Outdoor", "Hydration", 9.00, 29.00),
    ("P012", "Camp LED Lantern", "Outdoor", "Lighting", 17.00, 49.00),
    ("P013", "Motion Training Shoes", "Apparel", "Footwear", 34.00, 89.00),
    ("P014", "Core Performance Tee", "Apparel", "Clothing", 11.00, 34.00),
    ("P015", "Flex Travel Pants", "Apparel", "Clothing", 22.00, 64.00),
    ("P016", "Cloud Everyday Hoodie", "Apparel", "Clothing", 26.00, 74.00),
]

REGIONS = ("North", "South", "East", "West")
SEGMENTS = ("Consumer", "Small Business", "Enterprise")
CHANNELS = ("Online", "Retail", "Partner")
PAYMENT_MODES = ("Card", "UPI", "Bank Transfer", "Wallet")


def write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def generate_customers() -> list[dict]:
    rows = []
    start_date = date(2022, 1, 1)
    for index in range(1, 81):
        segment = SEGMENTS[(index * 7) % len(SEGMENTS)]
        region = REGIONS[(index * 5) % len(REGIONS)]
        rows.append(
            {
                "customer_id": f"C{index:03d}",
                "customer_name": f"Customer {index:03d}",
                "segment": segment,
                "region": region,
                "join_date": (start_date + timedelta(days=index * 11)).isoformat(),
            }
        )
    return rows


def generate_products() -> list[dict]:
    return [
        {
            "product_id": product_id,
            "product_name": product_name,
            "category": category,
            "sub_category": sub_category,
            "unit_cost": f"{unit_cost:.2f}",
            "list_price": f"{list_price:.2f}",
        }
        for product_id, product_name, category, sub_category, unit_cost, list_price in PRODUCTS
    ]


def order_status(rng: random.Random) -> str:
    value = rng.random()
    if value < 0.025:
        return "CANCELLED"
    if value < 0.065:
        return "RETURNED"
    return "COMPLETED"


def generate_order_lines(
    customers: list[dict],
    products: list[dict],
) -> tuple[list[dict], list[dict]]:
    rng = random.Random(SEED)
    rows = []
    valid_rows = []
    start_date = date(2025, 1, 1)

    category_weights_by_region = {
        "North": {"Electronics": 1.5, "Home Office": 1.2, "Outdoor": 0.8, "Apparel": 1.0},
        "South": {"Electronics": 0.9, "Home Office": 0.8, "Outdoor": 1.2, "Apparel": 1.5},
        "East": {"Electronics": 1.2, "Home Office": 1.4, "Outdoor": 0.8, "Apparel": 1.0},
        "West": {"Electronics": 1.4, "Home Office": 1.0, "Outdoor": 1.3, "Apparel": 0.9},
    }

    for order_number in range(1, 961):
        progress = (order_number - 1) / 959
        day_offset = min(364, int((progress**0.82) * 365))
        order_date = start_date + timedelta(days=day_offset)
        customer = rng.choice(customers)
        channel = rng.choices(CHANNELS, weights=(0.53, 0.30, 0.17), k=1)[0]
        payment_mode = rng.choice(PAYMENT_MODES)
        status = order_status(rng)
        line_count = rng.choices((1, 2, 3), weights=(0.42, 0.38, 0.20), k=1)[0]

        category_weights = category_weights_by_region[customer["region"]]
        product_weights = [
            category_weights[product["category"]] for product in products
        ]
        chosen_products = []
        while len(chosen_products) < line_count:
            product = rng.choices(products, weights=product_weights, k=1)[0]
            if product["product_id"] not in {item["product_id"] for item in chosen_products}:
                chosen_products.append(product)

        for line_number, product in enumerate(chosen_products, start=1):
            quantity = rng.choices((1, 2, 3, 4, 5), weights=(0.36, 0.29, 0.19, 0.11, 0.05), k=1)[0]
            list_price = float(product["list_price"])
            price_factor = 1 + ((order_date.month - 1) * 0.002) + rng.uniform(-0.015, 0.015)
            unit_price = round(list_price * price_factor, 2)

            base_discount = {"Online": 0.04, "Retail": 0.02, "Partner": 0.07}[channel]
            segment_discount = {"Consumer": 0.00, "Small Business": 0.02, "Enterprise": 0.05}[
                customer["segment"]
            ]
            discount_pct = min(0.18, round(base_discount + segment_discount + rng.choice((0, 0.01, 0.02)), 4))

            row = {
                "order_line_id": f"OL{order_number:05d}-{line_number}",
                "order_id": f"O{order_number:05d}",
                "order_date": order_date.isoformat(),
                "customer_id": customer["customer_id"],
                "product_id": product["product_id"],
                "quantity": quantity,
                "unit_price": f"{unit_price:.2f}",
                "discount_pct": f"{discount_pct:.4f}",
                "sales_channel": channel,
                "payment_mode": payment_mode,
                "order_status": status,
            }
            rows.append(row)

            if status != "CANCELLED":
                valid_rows.append(
                    {
                        **row,
                        "region": customer["region"],
                        "segment": customer["segment"],
                        "category": product["category"],
                        "product_name": product["product_name"],
                        "unit_cost": float(product["unit_cost"]),
                    }
                )

    rows.extend(
        [
            {
                "order_line_id": "BAD-001",
                "order_id": "",
                "order_date": "2025-12-15",
                "customer_id": "C001",
                "product_id": "P001",
                "quantity": 1,
                "unit_price": "89.00",
                "discount_pct": "0.0500",
                "sales_channel": "Online",
                "payment_mode": "Card",
                "order_status": "COMPLETED",
            },
            {
                "order_line_id": "BAD-002",
                "order_id": "O-BAD-002",
                "order_date": "not-a-date",
                "customer_id": "C002",
                "product_id": "P002",
                "quantity": 2,
                "unit_price": "189.00",
                "discount_pct": "0.0500",
                "sales_channel": "Retail",
                "payment_mode": "UPI",
                "order_status": "COMPLETED",
            },
            {
                "order_line_id": "BAD-003",
                "order_id": "O-BAD-003",
                "order_date": "2025-12-16",
                "customer_id": "C003",
                "product_id": "P003",
                "quantity": 0,
                "unit_price": "239.00",
                "discount_pct": "0.0500",
                "sales_channel": "Partner",
                "payment_mode": "Bank Transfer",
                "order_status": "COMPLETED",
            },
        ]
    )
    return rows, valid_rows


def signed_financials(row: dict) -> tuple[float, float, float]:
    quantity = int(row["quantity"])
    gross_sales = quantity * float(row["unit_price"])
    net_before_sign = round(gross_sales * (1 - float(row["discount_pct"])), 2)
    cost_before_sign = round(quantity * float(row["unit_cost"]), 2)
    sign = -1 if row["order_status"] == "RETURNED" else 1
    net_sales = sign * net_before_sign
    cost_amount = sign * cost_before_sign
    gross_profit = round(net_sales - cost_amount, 2)
    return net_sales, cost_amount, gross_profit


def generate_targets(valid_rows: list[dict]) -> list[dict]:
    actuals = defaultdict(float)
    for row in valid_rows:
        month_start = row["order_date"][:7] + "-01"
        net_sales, _, _ = signed_financials(row)
        actuals[(month_start, row["region"])] += net_sales

    rows = []
    region_adjustment = {"North": 1.00, "South": 1.04, "East": 1.01, "West": 0.97}
    margin_targets = {"North": 0.30, "South": 0.29, "East": 0.31, "West": 0.32}
    for month in range(1, 13):
        month_start = date(2025, month, 1).isoformat()
        if month <= 4:
            period_factor = 1.07
        elif month <= 8:
            period_factor = 1.00
        else:
            period_factor = 0.94
        for region in REGIONS:
            actual = actuals[(month_start, region)]
            target = max(2500.0, actual * period_factor * region_adjustment[region])
            rows.append(
                {
                    "month_start": month_start,
                    "region": region,
                    "revenue_target": f"{target:.2f}",
                    "margin_target_pct": f"{margin_targets[region]:.4f}",
                }
            )
    return rows


def write_expected_results(valid_rows: list[dict], targets: list[dict]) -> None:
    revenue_by_region = defaultdict(float)
    revenue_by_category = defaultdict(float)
    revenue_by_product = defaultdict(float)
    profit_by_region = defaultdict(float)
    profit_by_category = defaultdict(float)
    profit_by_product = defaultdict(float)
    revenue_by_period = defaultdict(float)
    total_revenue = 0.0
    total_profit = 0.0
    completed_order_ids = set()
    customer_ids = set()

    for row in valid_rows:
        net_sales, _, gross_profit = signed_financials(row)
        total_revenue += net_sales
        total_profit += gross_profit
        revenue_by_region[row["region"]] += net_sales
        revenue_by_category[row["category"]] += net_sales
        revenue_by_product[row["product_name"]] += net_sales
        profit_by_region[row["region"]] += gross_profit
        profit_by_category[row["category"]] += gross_profit
        profit_by_product[row["product_name"]] += gross_profit
        period = "Jan-Apr" if int(row["order_date"][5:7]) <= 4 else "Sep-Dec" if int(row["order_date"][5:7]) >= 9 else "May-Aug"
        revenue_by_period[period] += net_sales
        completed_order_ids.add(row["order_id"])
        customer_ids.add(row["customer_id"])

    target_by_period = defaultdict(float)
    total_target = 0.0
    for row in targets:
        target = float(row["revenue_target"])
        total_target += target
        month = int(row["month_start"][5:7])
        period = "Jan-Apr" if month <= 4 else "Sep-Dec" if month >= 9 else "May-Aug"
        target_by_period[period] += target

    gross_margin = total_profit / total_revenue if total_revenue else 0
    attainment = total_revenue / total_target if total_target else 0
    top_region = max(revenue_by_region, key=revenue_by_region.get)
    top_category = max(revenue_by_category, key=revenue_by_category.get)
    early_attainment = revenue_by_period["Jan-Apr"] / target_by_period["Jan-Apr"]
    late_attainment = revenue_by_period["Sep-Dec"] / target_by_period["Sep-Dec"]

    kpi_rows = [
        {"metric": "Total Revenue", "expected_value": f"{total_revenue:.2f}"},
        {"metric": "Gross Profit", "expected_value": f"{total_profit:.2f}"},
        {"metric": "Gross Margin Pct", "expected_value": f"{gross_margin:.4f}"},
        {"metric": "Distinct Orders", "expected_value": len(completed_order_ids)},
        {"metric": "Distinct Customers", "expected_value": len(customer_ids)},
        {"metric": "Revenue Target", "expected_value": f"{total_target:.2f}"},
        {"metric": "Target Attainment Pct", "expected_value": f"{attainment:.4f}"},
        {"metric": "Top Region", "expected_value": top_region},
        {"metric": "Top Category", "expected_value": top_category},
    ]
    write_csv(DATA_DIR / "expected_story_kpis.csv", ["metric", "expected_value"], kpi_rows)

    breakdown_rows = []
    for dimension_name, revenue_values, profit_values in (
        ("Region", revenue_by_region, profit_by_region),
        ("Category", revenue_by_category, profit_by_category),
        ("Product", revenue_by_product, profit_by_product),
    ):
        for member, revenue in sorted(revenue_values.items(), key=lambda item: item[1], reverse=True):
            profit = profit_values[member]
            breakdown_rows.append(
                {
                    "dimension": dimension_name,
                    "member": member,
                    "total_revenue": f"{revenue:.2f}",
                    "gross_profit": f"{profit:.2f}",
                    "gross_margin_pct": f"{profit / revenue:.4f}" if revenue else "0.0000",
                }
            )
    write_csv(
        DATA_DIR / "expected_story_breakdown.csv",
        ["dimension", "member", "total_revenue", "gross_profit", "gross_margin_pct"],
        breakdown_rows,
    )

    insight_text = f"""# Expected Demo Story

These values are generated from the same deterministic source records used by the Snowflake demo.

- Total revenue: ${total_revenue:,.2f}
- Gross profit: ${total_profit:,.2f}
- Gross margin: {gross_margin:.1%}
- Orders represented: {len(completed_order_ids):,}
- Customers represented: {len(customer_ids):,}
- Full-year target attainment: {attainment:.1%}
- Top revenue region: {top_region}
- Top revenue category: {top_category}
- January-April target attainment: {early_attainment:.1%}
- September-December target attainment: {late_attainment:.1%}

## Narrative

The business begins below target, improves through the middle of the year, and closes the year above target.
Use region, category, segment, and channel filters to explain which combinations produced the recovery.
Returned orders are retained as negative revenue so that the report tells a financially honest story.
"""
    (DATA_DIR / "expected_story_insights.md").write_text(insight_text, encoding="utf-8")


def main() -> None:
    customers = generate_customers()
    products = generate_products()
    order_lines, valid_rows = generate_order_lines(customers, products)
    targets = generate_targets(valid_rows)

    write_csv(
        DATA_DIR / "customers.csv",
        ["customer_id", "customer_name", "segment", "region", "join_date"],
        customers,
    )
    write_csv(
        DATA_DIR / "products.csv",
        ["product_id", "product_name", "category", "sub_category", "unit_cost", "list_price"],
        products,
    )
    write_csv(
        DATA_DIR / "order_lines.csv",
        [
            "order_line_id",
            "order_id",
            "order_date",
            "customer_id",
            "product_id",
            "quantity",
            "unit_price",
            "discount_pct",
            "sales_channel",
            "payment_mode",
            "order_status",
        ],
        order_lines,
    )
    write_csv(
        DATA_DIR / "monthly_targets.csv",
        ["month_start", "region", "revenue_target", "margin_target_pct"],
        targets,
    )
    write_expected_results(valid_rows, targets)

    print(f"Generated demo files in {DATA_DIR}")
    print(f"Customers: {len(customers)}")
    print(f"Products: {len(products)}")
    print(f"Order lines including quality-test rows: {len(order_lines)}")
    print(f"Monthly regional targets: {len(targets)}")


if __name__ == "__main__":
    main()
