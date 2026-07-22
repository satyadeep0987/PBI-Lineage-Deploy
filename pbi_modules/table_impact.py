import re


MEASURE_TABLE_FIELDS = (
    "Semantic Table/View",
    "Exact Source Table/View",
    "Fully Qualified Source Object",
)

SOURCE_TABLE_FIELDS = (
    "Power BI Table Name",
    "Source Name",
    "Fully Qualified Name",
)


def normalize_identifier(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _identifier_parts(value):
    text = str(value or "").strip().strip("'\"")
    text = text.replace("[", "").replace("]", "")
    return [part.strip().strip("'\"") for part in text.split(".") if part.strip()]


def table_value_matches(value, query, include_partial=False):
    """Match an unqualified table name or a qualified database/schema/table path."""
    query_parts = _identifier_parts(query)
    if not query_parts:
        return False

    query_full = normalize_identifier(".".join(query_parts))
    query_is_qualified = len(query_parts) > 1

    for candidate in re.split(r"\s*;\s*", str(value or "")):
        candidate_parts = _identifier_parts(candidate)
        if not candidate_parts:
            continue
        candidate_full = normalize_identifier(".".join(candidate_parts))
        candidate_leaf = normalize_identifier(candidate_parts[-1])

        if query_is_qualified:
            if candidate_full == query_full or candidate_full.endswith(query_full):
                return True
        elif candidate_leaf == query_full or candidate_full == query_full:
            return True

        if include_partial and query_full in candidate_full:
            return True

    return False


def find_table_match(record, query, fields, include_partial=False):
    for field in fields:
        value = (record or {}).get(field)
        if table_value_matches(value, query, include_partial=include_partial):
            return field, str(value)
    return None, None
