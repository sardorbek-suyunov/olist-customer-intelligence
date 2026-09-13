{{ config(
    materialized='table',
    cluster_by=(['customer_unique_id'] if target.type == 'bigquery' else none)
) }}

{#
    NOT PARTITIONED, for two independent reasons.

    1. It would be rejected. BigQuery time-unit partitioning only accepts
       values in 1960-01-01 .. 2159-12-31, and version 1's valid_from is
       floored to 1900-01-01 (see var scd_valid_from_floor). Partitioning on
       valid_from would put almost every row outside the legal range, since
       version 1 is the overwhelming majority of the table.

    2. It would be pointless even if legal. The table is single-digit MB.
       BigQuery guidance is roughly 1 GB per partition; partitioning a
       dimension this small produces metadata overhead and slower scans, not
       faster ones.

    Clustering on customer_unique_id is the right tool at this size: it is free,
    has no minimum, and is what the as-of join actually probes on.
#}

/*
    Type 2 customer dimension at customer_unique_id grain.

    Row counts and change-event totals: docs/figures.json.

    Validity intervals are HALF-OPEN: [valid_from, valid_to). This matters --
    290 (customer, timestamp) pairs are exact ties, and a closed interval would
    let one order match two versions and silently fan out fct_orders.

    Version 1's valid_from is floored to var('scd_valid_from_floor') rather
    than the customer's first order timestamp, so a late-arriving backdated
    order still resolves to version 1 instead of falling through the as-of
    join and dropping the row.
*/

with versions as (

    select * from {{ ref('int_customer_address_versions') }}

),

windowed as (

    select
        *,
        lead(version_started_at) over (
            partition by customer_unique_id
            order by version_number
        ) as next_version_started_at
    from versions

)

select
    {{ dbt_utils.generate_surrogate_key(['customer_unique_id', 'version_number']) }} as customer_sk,
    customer_unique_id,
    version_number,
    customer_zip_code_prefix,
    customer_city,
    customer_state,

    case
        when version_number = 1
            then cast('{{ var("scd_valid_from_floor") }}' as {{ dbt.type_timestamp() }})
        else version_started_at
    end as valid_from,

    coalesce(
        next_version_started_at,
        cast('{{ var("scd_valid_to_ceiling") }}' as {{ dbt.type_timestamp() }})
    ) as valid_to,

    (next_version_started_at is null) as is_current,
    version_started_at   as first_observed_at,
    observations_in_version as orders_in_version

from windowed
