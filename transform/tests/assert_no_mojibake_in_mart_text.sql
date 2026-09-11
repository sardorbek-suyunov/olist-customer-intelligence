/*
    MOJIBAKE CONTAINMENT.

    Three geolocation city values are double-encoded UTF-8 that was then
    accent-stripped, leaving a bare Latin-1 continuation byte:

        'maceia(U+00B3)'    is 'maceio' with an acute  (bytes C3 B3)
        'sa(U+00A3)o paulo' is 'sao paulo' with a tilde (bytes C3 A3)
        '(U+00B4)teresopolis'

    Normalization deletes the stray byte rather than decoding it, so
    'maceia(U+00B3)' becomes 'maceia' -- which is consistently wrong, because
    the real name is 'maceio'. That is accepted rather than repaired, because
    measurement says the blast radius is nil:

      * customers  : 0 mojibake rows
      * sellers    : 2 rows, 1 distinct value ('santa barbara d(U+00B4)oeste'),
                     and normalization ALREADY folds it onto the same canonical
                     'santa barbara d oeste' as the other two spellings -- it
                     is one of the 611 -> 603 seller-city merges
      * geolocation: 3 rows, in a column stg_geolocation computes in a CTE and
                     then discards; that model is keyed on zip_code_prefix
      * city is never a JOIN key or a GROUP BY key in any model

    So no aggregate splits one city into two, and no join drops a row. Writing
    a byte-level repair into hot-path SQL to fix three values nothing reads
    would be the wrong trade.

    What this test does is stop that reasoning from silently expiring. If a
    mojibake value ever reaches a normalized mart column -- because the source
    changed, or because someone projects geolocation city into a mart -- the
    build fails and the decision gets revisited against real numbers instead
    of against a stale comment.
*/

with mart_text as (

    select 'dim_customers.customer_city' as column_name, customer_city as value
    from {{ ref('dim_customers') }}

    union all
    select 'dim_sellers.seller_city', seller_city
    from {{ ref('dim_sellers') }}

)

select
    column_name,
    value
from mart_text
where value is not null
  -- Normalization deletes every non-ASCII character, so a NORMALIZED mart
  -- column must contain none at all. Asserting the whole class is both
  -- stronger than a mojibake-range regex and portable: no \u escapes, and no
  -- ordinal-indicator carve-out to keep in sync.
  and {{ contains_non_ascii('value') }}
