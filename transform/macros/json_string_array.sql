{#
    Extract a JSON array of strings into something UNNEST can consume.

    The aspect list is stored as a JSON array because the label is one row per
    review and the taxonomy is a set. Reading it back needs one function that
    the two engines spell differently, and nothing else about the unnest
    differs -- `from t, unnest(...) as a(x)` parses identically on both, which
    is why only the extraction is dispatched and not the whole statement.

    The aspect NAMES are not repeated here or anywhere else in SQL. They live in
    enrichment/taxonomy.py and reach dbt as a seed, so a taxonomy change cannot
    leave a stale copy behind in a CASE expression.
#}

{% macro json_string_array(column) %}
    {%- if target.type == 'bigquery' -%}
        json_value_array({{ column }}, '$')
    {%- else -%}
        json_extract_string({{ column }}, '$[*]')
    {%- endif -%}
{% endmacro %}
