{#
    Unnest a JSON array of strings into rows, in the target's own dialect.

    TWO THINGS DIFFER, AND ONLY ONE OF THEM IS OBVIOUS.

    The extraction function differs, which is what this macro was originally
    written for:
        duckdb    json_extract_string(col, '$[*]')
        bigquery  json_value_array(col, '$')

    The LATERAL ALIAS also differs, which is what it originally got wrong:
        duckdb    ... as a(aspect)     -- Postgres-style, names the column
        bigquery  ... as aspect        -- rejects the parenthesised form outright

    The first version emitted only the extraction and left `as a(aspect)` in the
    model, having verified that form "works on both". It was verified on DuckDB
    alone -- BigQuery answers it with `Syntax error: Expected ")" but got "("` --
    and the mistake survived because the BigQuery build was separately broken by
    an unset GCP_PROJECT_ID, so the model had never actually been compiled there.
    A cross-engine claim tested on one engine is not a cross-engine claim.

    So the macro now renders the WHOLE clause. There is nothing dialect-specific
    left in the model for a future edit to get wrong, which is the point: the
    model says what it wants, the macro knows how each engine spells it.

    The aspect NAMES are still not repeated in SQL anywhere -- they live in
    enrichment/taxonomy.py and reach dbt as a generated seed.
#}

{% macro unnest_json_array(column, alias) %}
    {%- if target.type == 'bigquery' -%}
        unnest(json_value_array({{ column }}, '$')) as {{ alias }}
    {%- else -%}
        unnest(json_extract_string({{ column }}, '$[*]')) as unnested({{ alias }})
    {%- endif -%}
{% endmacro %}
