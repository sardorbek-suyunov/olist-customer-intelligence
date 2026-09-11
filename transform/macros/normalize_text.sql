{#
    Canonicalise a free-text location string before it is used in a
    change-detection hash.

    THE ALGORITHM, stated once because it is implemented three times (DuckDB
    SQL, BigQuery SQL, and Python in scripts/profile_dataset.py):

      1. NFD normalize and remove combining marks   ('são' -> 'sao')
      2. lowercase
      3. SPACING ACCENT CHARACTERS -> a space
      4. DELETE every other remaining non-ASCII character
      5. replace each remaining [^a-z0-9 ] run with a single space
      6. collapse whitespace, trim, empty string -> null

    Step 1 must be NFD, NOT NFKD. Compatibility decomposition would turn
    '4\x{00BA} centenario' into '4o centenario' and 'maceia\x{00B3}' into
    'maceia3', inventing characters the name never had.

    Step 3 exists because NFD then leaves the standalone spacing accents
    (U+00B4 ACUTE, U+00A8 DIAERESIS, U+00B8 CEDILLA, ...) intact, and step 4
    would delete them outright -- turning 'santa barbara d\x{00B4}oeste' into
    'santa barbara doeste', which then does NOT merge with the correctly
    spelled "d'oeste" and "d oeste" and silently splits one city into two.
    These characters are typographic stand-ins for an apostrophe in this data,
    so they behave like punctuation: they become a space.

    The distinction between steps 3/5 (-> space) and step 4 (delete) is
    load-bearing:
      'mogi-guacu'                -> 'mogi guacu'   hyphen separates words
      'santa barbara d\x{00B4}oeste' -> 'santa barbara d oeste'
      'sa\x{00A3}o paulo'         -> 'sao paulo'    mojibake byte is noise;
                                                    deleting it merges the row
                                                    with the real 'sao paulo'

    MEASURED EFFECT:
      customers : 0 spurious version boundaries removed -- the source already
                  ships lowercased and ASCII-folded. This is a guard.
      sellers   : 611 distinct city strings collapse to 603. Load-bearing.

    Verified identical across all three implementations by
    assert_normalize_macro_matches_python (DuckDB, 12,818 strings) and
    scripts/validate_bigquery.py (BigQuery).
#}

{#  Standalone spacing accents that act as punctuation in this dataset.
    U+00A8 diaeresis, U+00AF macron, U+00B4 acute, U+00B8 cedilla,
    U+02C6 circumflex, U+02DC small tilde. #}
{% macro spacing_accent_class() %}{{ '[\\x{00A8}\\x{00AF}\\x{00B4}\\x{00B8}\\x{02C6}\\x{02DC}]' }}{% endmacro %}


{% macro normalize_text(col) %}
    {{ return(adapter.dispatch('normalize_text', 'olist_intelligence')(col)) }}
{% endmacro %}


{% macro duckdb__normalize_text(col) %}
    nullif(
        trim(
            regexp_replace(
                regexp_replace(
                    regexp_replace(
                        regexp_replace(
                            lower(strip_accents(cast({{ col }} as varchar))),
                            '{{ spacing_accent_class() }}', ' ', 'g'
                        ),
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
                        regexp_replace(
                            lower(regexp_replace(normalize(cast({{ col }} as string), NFD), r'\p{Mn}', '')),
                            r'{{ spacing_accent_class() }}', ' '
                        ),
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
