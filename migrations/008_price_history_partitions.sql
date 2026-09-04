-- price_daily holds raw market history that predates the first scoring date:
-- momentum needs at least 12 months of trailing prices, so the first real
-- ingest in 2026 writes 2025 (and earlier) rows. Pre-create yearly partitions
-- back to 2020 so those inserts don't fail. Empty partitions cost almost
-- nothing at ~756k rows/yr for the full universe.
create table price_daily_2020 partition of price_daily
    for values from ('2020-01-01') to ('2021-01-01');
create table price_daily_2021 partition of price_daily
    for values from ('2021-01-01') to ('2022-01-01');
create table price_daily_2022 partition of price_daily
    for values from ('2022-01-01') to ('2023-01-01');
create table price_daily_2023 partition of price_daily
    for values from ('2023-01-01') to ('2024-01-01');
create table price_daily_2024 partition of price_daily
    for values from ('2024-01-01') to ('2025-01-01');
create table price_daily_2025 partition of price_daily
    for values from ('2025-01-01') to ('2026-01-01');
