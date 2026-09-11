/*
    CROSS-LANGUAGE PARITY.

    The normalize_text macro (SQL, per-adapter) and normalize() in
    scripts/profile_dataset.py (Python) implement the same transformation.
    Two implementations that must agree are a defect waiting to happen, and a
    docstring saying "keep these in sync" is not a control.

    The seed holds every distinct location string in the source (customers,
    sellers and geolocation cities and states) alongside its Python-normalized
    form. This test pushes each raw value back through the macro in the
    TARGET'S OWN DIALECT and fails on any disagreement -- so duckdb's
    strip_accents path and bigquery's NORMALIZE(NFD) path are each verified
    against the same reference, and a divergence between the two adapters is
    caught as well.

    Regenerate the seed with `make profile`.
*/

with reference as (

    select
        source_column,
        raw_value,
        python_normalized
    from {{ ref('normalization_parity') }}

),

recomputed as (

    select
        source_column,
        raw_value,
        python_normalized,
        {{ normalize_text('raw_value') }} as macro_normalized
    from reference

)

select
    source_column,
    raw_value,
    python_normalized,
    macro_normalized
from recomputed
-- Null-safe inequality written out longhand rather than IS DISTINCT FROM,
-- so the test compiles identically on every adapter.
where (python_normalized is null) != (macro_normalized is null)
   or python_normalized != macro_normalized
