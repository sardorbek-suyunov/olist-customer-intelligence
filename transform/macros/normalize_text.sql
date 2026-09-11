{#
    Canonicalise a free-text location string before it is used in a
    change-detection hash.

    THE ALGORITHM, fixed in one place because it is implemented twice
    (here in SQL, and in scripts/profile_dataset.py in Python):

      1. NFD normalize and remove combining marks  ('sao' <- 'sao' w/ tilde)
      2. lowercase
      3. DELETE every remaining non-ASCII character  (not replace -- delete)
      4. replace each remaining [^a-z0-9 ] run with a single space
      5. collapse whitespace, trim, empty string -> null

    Step 3 is deliberately a deletion while step 4 is a replacement, and the
    distinction is load-bearing:
      'mogi-guacu'   -> 'mogi guacu'  (hyphen is a word separator)
      'sa\u00a3o paulo' -> 'sao paulo'   (mojibake byte is noise; deleting it
                                       merges the row with real 'sao paulo',
                                       replacing it would yield 'sa o paulo')

    Step 1 must be NFD, NOT NFKD. Compatibility decomposition would turn
    '4\u00ba centenario' into '4o centenario' and 'maceia\u00b3' into 'maceia3',
    inventing characters that were never in the name. assert_normalize_macro_
    matches_python caught exactly these three strings when the two
    implementations disagreed.

    MEASURED EFFECT:
      customers : 0 spurious version boundaries removed -- the source already
                  ships lowercased and ASCII-folded. This is a guard.
      sellers   : 611 distinct city strings collapse to 603. Load-bearing.
#}
{% macro normalize_text(col) %}
    {{ return(adapter.dispatch('normalize_text', 'olist_intelligence')(col)) }}
{% endmacro %}


{% macro duckdb__normalize_text(col) %}
    nullif(
        trim(
            regexp_replace(
                regexp_replace(
                    regexp_replace(
                        lower(strip_accents(cast({{ col }} as varchar))),
                        '[^\x00-\x7F]', '', 'g'
                    ),
                    '[^a-z0-9 ]+', ' ', 'g'
                ),
                '\s+', ' ', 'g'
            )
        ),
        ''
    )
{% endmacro %}


{% macro bigquery__normalize_text(col) %}
    nullif(
        trim(
            regexp_replace(
                regexp_replace(
                    regexp_replace(
                        lower(regexp_replace(normalize(cast({{ col }} as string), NFD), r'\p{Mn}', '')),
                        r'[^\x00-\x7F]', ''
                    ),
                    r'[^a-z0-9 ]+', ' '
                ),
                r'\s+', ' '
            )
        ),
        ''
    )
{% endmacro %}


{#  Deterministic surrogate/attribute key. Kept local rather than pulling in
    dbt_utils so the core models have no package dependency.

    Dispatched because md5() is not portable: DuckDB returns a hex VARCHAR,
    BigQuery returns BYTES. Left undispatched, customer_sk would silently
    become a BYTES column on BigQuery and any join against a STRING key would
    fail at runtime rather than at compile time. #}
{% macro surrogate_key(columns) %}
    {{ return(adapter.dispatch('surrogate_key', 'olist_intelligence')(columns)) }}
{% endmacro %}


{% macro _concat_for_hash(columns) %}
    {%- for c in columns %}
    coalesce(cast({{ c }} as {{ dbt.type_string() }}), '<null>')
    {%- if not loop.last %} || '|' || {% endif %}
    {%- endfor %}
{% endmacro %}


{% macro duckdb__surrogate_key(columns) %}
    md5({{ _concat_for_hash(columns) }})
{% endmacro %}


{% macro bigquery__surrogate_key(columns) %}
    to_hex(md5({{ _concat_for_hash(columns) }}))
{% endmacro %}


{#  Portable "does this string contain a non-ASCII character?".
    DuckDB spells it regexp_matches, BigQuery regexp_contains -- an
    undispatched call is a compile error on the other adapter. #}
{% macro contains_non_ascii(col) %}
    {{ return(adapter.dispatch('contains_non_ascii', 'olist_intelligence')(col)) }}
{% endmacro %}

{% macro duckdb__contains_non_ascii(col) %}
    regexp_matches({{ col }}, '[^\x00-\x7F]')
{% endmacro %}

{% macro bigquery__contains_non_ascii(col) %}
    regexp_contains({{ col }}, r'[^\x00-\x7F]')
{% endmacro %}
