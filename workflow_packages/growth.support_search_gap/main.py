"""Join Search Console demand with support-inbox volume to find content gaps.

Deterministic pull, rank and gate math lives here in Python; one narrow model
call proposes a Gmail search phrase per top query, nothing more.
"""

from datetime import datetime, timedelta

# A row below either threshold is real search demand OR real ticket volume
# alone -- not evidence of a gap between them. The 20-actors-per-arm floor in
# product.analytics_brief/skills/product-analytics/STATISTICS.md is the same
# idea applied to a different signal: pick a bound that keeps a screening
# result honest, then say "insufficient data" below it instead of a shaky claim.
MIN_GSC_IMPRESSIONS = 10
MIN_GMAIL_RESULT_ESTIMATE = 3

# One Search Console call asks for this many rows; the service binding's
# max_response_bytes clamps what actually comes back. Python then ranks the
# returned rows by impressions and keeps the top row_limit.
GSC_PULL_ROW_LIMIT = 1000

# Gmail's resultSizeEstimate does not depend on max_results, so this stays at
# the allowed minimum: each call moves as little message data as possible,
# and the returned message stubs are never read.
GMAIL_MAX_RESULTS = 1

PHRASE_INSTRUCTIONS = (
    "For each supplied Search Console query, propose exactly one Gmail search phrase "
    "that would find a support email about the same underlying need (for example, "
    "\"export my data csv\" could become 'subject:(export OR download) data'). Return "
    "every query exactly once, unchanged. Only map each query to a search phrase -- do "
    "not summarize, rank, explain or judge the queries."
)


def _window(ctx, lookback_days):
    created_at = datetime.fromisoformat(ctx["created_at"])
    end = created_at.date()
    start = end - timedelta(days=lookback_days)
    return start.isoformat(), end.isoformat()


def _top_queries(rows, row_limit):
    if not isinstance(rows, list):
        raise ValueError("Search Console response was not the expected shape")
    parsed = {}
    for row in rows:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("keys"), list)
            or len(row["keys"]) != 1
            or not isinstance(row["keys"][0], str)
            or not row["keys"][0].strip()
            or type(row.get("impressions")) not in (int, float)
            or row["impressions"] < 0
        ):
            raise ValueError("Search Console returned a malformed query row")
        query = row["keys"][0]
        impressions = int(row["impressions"])
        # A query should not repeat within one response; keep the larger count
        # defensively rather than trusting the provider's ordering silently.
        if query not in parsed or impressions > parsed[query]:
            parsed[query] = impressions
    ranked = sorted(parsed.items(), key=lambda pair: (-pair[1], pair[0]))
    return [
        {"query": query, "impressions": impressions} for query, impressions in ranked[:row_limit]
    ]


def _phrase_schema(count):
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "pairs": {
                "type": "array",
                "minItems": count,
                "maxItems": count,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "query": {"type": "string", "minLength": 1, "maxLength": 200},
                        "gmail_phrase": {"type": "string", "minLength": 1, "maxLength": 200},
                    },
                    "required": ["query", "gmail_phrase"],
                },
            }
        },
        "required": ["pairs"],
    }


def _derive_phrase_map(pairs, expected_queries):
    by_query = {}
    for pair in pairs:
        query = pair["query"]
        if query in by_query:
            raise ValueError("The model duplicated a query")
        by_query[query] = pair["gmail_phrase"]
    if set(by_query) != expected_queries:
        raise ValueError("The model changed the query set")
    return by_query


def _cell(text):
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def _table(rows):
    lines = [
        "| query | gsc_impressions | gmail_phrase | gmail_estimate | status |",
        "| --- | ---: | --- | ---: | --- |",
    ]
    lines += [
        f"| {_cell(r['query'])} | {r['gsc_impressions']} | {_cell(r['gmail_phrase'])} "
        f"| {r['gmail_estimate']} | {r['status']} |"
        for r in rows
    ]
    return lines


def _render(mode, start_date, end_date, rows):
    if mode == "moneyball":
        return "\n".join(_table(rows)) + "\n"
    lines = [
        "# Support/search demand gap",
        "",
        f"Window: {start_date} to {end_date} (exclusive)",
        "",
        *_table(rows),
        "",
    ]
    cleared = [r for r in rows if r["status"] == "content gap"]
    if cleared:
        lines.append("## Findings")
        lines.append("")
        lines += [
            f'- "{_cell(r["query"])}" draws {r["gsc_impressions"]} Search Console impressions '
            f"and coincides with an independent Gmail signal (~{r['gmail_estimate']} messages "
            f'matching "{_cell(r["gmail_phrase"])}") -- a demand correlation, not evidence that '
            "either caused the other."
            for r in cleared
        ]
    elif rows:
        lines.append("No row cleared both thresholds; no correlation claim is supported.")
    else:
        lines.append("No Search Console queries were returned for this window.")
    lines.append("")
    return "\n".join(lines)


async def run(ctx, inputs):
    row_limit = inputs["row_limit"]
    start_date, end_date = _window(ctx, inputs["lookback_days"])
    mode = inputs["mode"]

    gsc_response = await ctx.services.call(
        service="gsc",
        step="gsc_query_pull",
        operation="search_analytics.read",
        arguments={
            "start_date": start_date,
            "end_date": end_date,
            "dimensions": ["query"],
            "row_limit": GSC_PULL_ROW_LIMIT,
        },
    )
    if not isinstance(gsc_response, dict):
        raise ValueError("Search Console response was not the expected shape")
    top_queries = _top_queries(gsc_response.get("rows", []), row_limit)

    if not top_queries:
        return {
            "path": "reports/SUPPORT_SEARCH_GAP.md",
            "content": _render(mode, start_date, end_date, []),
        }

    model_response = await ctx.models.generate(
        route="derive_phrases",
        step="derive_gmail_phrases",
        instructions=PHRASE_INSTRUCTIONS,
        data=[item["query"] for item in top_queries],
        output_schema=_phrase_schema(len(top_queries)),
    )
    pairs = model_response["parsed"]["pairs"]
    if len(pairs) != len(top_queries):
        raise ValueError("The model changed the number of queries")
    phrase_by_query = _derive_phrase_map(pairs, {item["query"] for item in top_queries})

    rows = []
    for index, item in enumerate(top_queries):
        phrase = phrase_by_query[item["query"]]
        gmail_response = await ctx.services.call(
            service="gmail",
            step=f"gmail_search_{index}",
            operation="gmail.messages.search",
            arguments={"query": phrase, "max_results": GMAIL_MAX_RESULTS},
        )
        if (
            not isinstance(gmail_response, dict)
            or type(gmail_response.get("result_size_estimate")) is not int
        ):
            raise ValueError("Gmail search response was not the expected shape")
        estimate = gmail_response["result_size_estimate"]
        status = (
            "content gap"
            if item["impressions"] >= MIN_GSC_IMPRESSIONS and estimate >= MIN_GMAIL_RESULT_ESTIMATE
            else "insufficient data"
        )
        rows.append(
            {
                "query": item["query"],
                "gsc_impressions": item["impressions"],
                "gmail_phrase": phrase,
                "gmail_estimate": estimate,
                "status": status,
            }
        )

    return {
        "path": "reports/SUPPORT_SEARCH_GAP.md",
        "content": _render(mode, start_date, end_date, rows),
    }
