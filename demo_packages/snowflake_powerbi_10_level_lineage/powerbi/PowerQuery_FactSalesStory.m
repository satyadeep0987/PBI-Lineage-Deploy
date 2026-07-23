let
    // Replace the server value with the hostname shown in your Snowflake account.
    Source = Snowflake.Databases(
        "YOUR_ACCOUNT_IDENTIFIER.snowflakecomputing.com",
        "PBI_LINEAGE_DEMO_WH",
        [
            Implementation = "2.0",
            Role = "PBI_LINEAGE_DEMO_ROLE"
        ]
    ),
    DemoDatabase = Source{[Name = "PBI_LINEAGE_DEMO", Kind = "Database"]}[Data],
    MartSchema = DemoDatabase{[Name = "MART", Kind = "Schema"]}[Data],
    FactSalesStory = MartSchema{
        [Name = "FACT_PBI_SALES_STORY", Kind = "Table"]
    }[Data],
    TypedColumns = Table.TransformColumnTypes(
        FactSalesStory,
        {
            {"ORDER_DATE", type date},
            {"MONTH_START", type date},
            {"CUSTOMER_JOIN_DATE", type date},
            {"QUANTITY", Int64.Type},
            {"SALES_YEAR", Int64.Type},
            {"SALES_QUARTER", Int64.Type},
            {"IS_RETURNED", Int64.Type},
            {"IS_HIGH_VALUE_ORDER", Int64.Type},
            {"GROSS_SALES", Currency.Type},
            {"DISCOUNT_AMOUNT", Currency.Type},
            {"NET_SALES", Currency.Type},
            {"COST_AMOUNT", Currency.Type},
            {"GROSS_PROFIT", Currency.Type},
            {"ORDER_NET_SALES", Currency.Type},
            {"REGION_MONTH_REVENUE_TARGET", Currency.Type},
            {"REVENUE_TARGET_ALLOCATED", Currency.Type},
            {"TARGET_GAP_ALLOCATED", Currency.Type},
            {"MARGIN_PCT", Percentage.Type},
            {"MARGIN_TARGET_PCT", Percentage.Type}
        }
    )
in
    TypedColumns
