{{ config(materialized='view') }}

/*
    The shipped label run, and nothing else.

    Grain: one row per review_id. The enrichment store deliberately holds more
    than one run -- the v1 corpus, a v2 sample from the prompt iteration, and a
    reference model's labels used to score them. Reading it without a filter
    would fan a review out to three rows carrying three different opinions, and
    every downstream count would silently multiply.

    The filter is on the two vars in dbt_project.yml rather than on literals
    here, because scripts/profile_dataset.py reads the same two values when it
    writes docs/figures.json. One definition, two consumers: the README cannot
    end up describing a different run from the one the marts were built on.

    Joined through content_hash, which encodes (text, prompt version, model).
    Two reviews with identical text share a row -- that is the cache working,
    and it is why 40,950 reviews with text cost 35,616 labels.
*/

select
    m.review_id,
    r.content_hash,
    r.prompt_version as label_prompt_version,
    r.model          as label_model,
    r.aspects        as label_aspects_json,
    r.sentiment      as label_sentiment,
    r.severity       as label_severity,
    r.no_content     as label_no_content

from {{ source('olist_enrichment', 'review_enrichment_map') }} m
inner join {{ source('olist_enrichment', 'review_enrichment') }} r
    on m.content_hash = r.content_hash

where r.model = '{{ var("shipped_label_model") }}'
  and r.prompt_version = '{{ var("shipped_prompt_version") }}'
