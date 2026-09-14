/*
    The labelled data reaching the marts must come from exactly ONE label run,
    and it must be the run dbt_project.yml says it is.

    Two failures this catches, both silent:

    1. MIXED PROVENANCE. The enrichment store holds the v1 corpus, a 600-review
       v2 sample from the prompt iteration, and a reference model's labels. If
       the staging filter ever breaks, aspect frequencies become a blend of two
       prompts and a real change in the data cannot be told apart from a prompt
       artefact. The numbers would still look completely ordinary.

    2. A TRUTHFUL-LOOKING BUT WRONG STAMP. fct_segment_aspect stamps every row
       from the vars rather than from the data, so the stamp is only honest if
       the data actually matches the vars. Stamping from the data instead would
       hide case 1 -- the stamp would faithfully report a mixture. So the mart
       states what it believes it shipped, and this test checks the belief.

    Returns a row -- and so fails -- when the data disagrees with the declaration.
*/

with provenance as (

    select distinct
        label_prompt_version,
        label_model
    from {{ ref('stg_review_enrichment') }}

),

-- Counted separately so ZERO fails too.
--
-- The first version of this test selected from `provenance` and filtered on its
-- own count. With an empty staging model that scans no rows and returns no
-- rows, which dbt reads as a pass -- so the one state where the marts are
-- entirely unlabelled was the one state it could not report. A test that cannot
-- fail on the worst input is the same shape as everything else in the failure
-- table: a green result describing something nobody checked.
tally as (

    select count(*) as distinct_runs from provenance

)

select
    cast(null as {{ dbt.type_string() }}) as label_prompt_version,
    cast(null as {{ dbt.type_string() }}) as label_model,
    case
        when distinct_runs = 0
            then 'no labelled data reaches the marts at all; run `make enrich-restore`'
        else 'staging holds more than one label run; the marts would blend prompts'
    end as failure
from tally
where distinct_runs <> 1

union all

select
    label_prompt_version,
    label_model,
    'staging holds a run the project does not declare in dbt_project.yml' as failure
from provenance
where label_prompt_version <> '{{ var("shipped_prompt_version") }}'
   or label_model <> '{{ var("shipped_label_model") }}'
