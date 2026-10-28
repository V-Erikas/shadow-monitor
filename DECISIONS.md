## Key design decisions

1. **Score legal entities, not listings.** Inspections target companies. Listings of a chain are aggregated (reviews, opening hours) under one company code before scoring.

2. **Official Google Places API, not scraping Google.** Respects Google's terms, gives structured
   data and costs nothing within the free tier (field mask + 900-request cap; a full run used 301).
   Dense grid cells are split recursively to get past the 60-results-per-query limit.

3. **Primary Google type decides the category.** Secondary types let in McDonald's as a café and
   car dealers as repair shops. Filtering on the primary type removed ~120 noisy listings.

4. **Layered matching with explicit confidence and a hard threshold (0.8).** Company codes printed
   on business websites are the strongest evidence; fuzzy names are checked against the Sodra
   activity code and rejected when ambiguous. Website codes from unrelated industries (web
   developers, Regitra, news sites) are not accepted on their own. Name matches agreed with website
   codes in 94% of the 99 cases where both existed: an accuracy estimate without manual labels.

5. **Three independent signals and tiers instead of one opaque score.** Staffing gap (peer model),
   opening-hours floor (arithmetic an analyst can check) and low wage. A lead needs two strong
   signals, or an extreme staffing gap labelled as thinner evidence; one signal only reaches the
   watchlist. The score is multiplied by match confidence so uncertain matches rank lower.

6. **Benefit of the doubt built into the numbers.** Declared staff = maximum of the last three
   months; one owner allowance in the hours rule; MB / sole-proprietor, name-only match, other
   registered activity and seasonality are shown as caveats on every lead.

7. **Peer model, not fixed ratios.** A robust Huber log-log regression of insured staff on reviews
   with per-category intercepts adapts to each category and is not dragged by outliers including the shadow cases it is meant to find.

8. **DuckDB as the store.** One file, fast SQL over millions of Sodra rows, no server, and the
   analyst can query it read-only from the app. Every run-specific table is keyed by `run_id`,
   so monthly runs compare with a join.

9. **Reproducible without credentials.** Raw Google responses and website scans are committed as
   snapshots and replayed when no API key is present. Each row records whether it came from a live
   call or a snapshot. Official data is public and downloaded fresh.

10. **Data quality as part of the pipeline.** 24 checks with error/warn severity, stored per run.
    An error marks the run `dq_failed` and the app tells the analyst not to rely on it.

## Alternatives rejected
| Scraping Google Maps (incl. "popular times") | Against Googles terms; brittle. Popular times would be a better busyness signal, so it is listed as a limitation |
| Matching by registered address | The registered seat is often an accountants or owners address; resolving JAR addresses needs three extra Address Register joins. Deferred to production |
| ML classifier for leads | No labelled outcomes (past inspections) to train or validate on; an unexplainable score is hard to defend to an analyst |
| Postgres + separate BI tool | More moving parts for a single-user, monthly batch workload; DuckDB + Streamlit is one container |
| Filtering Sodra to Vilnius-registered companies | A Vilnius café can belong to a company registered elsewhere; all municipalities are loaded |
| Registrų centras CSV downloads | The site served a maintenance page during development |

## Known limitations

- **Coverage of matching**: 409 of 1,013 listings matched. Café brand names rarely resemble legal
  names 39% matched; sole traders have anonymised registry names and no Sodra company record.
- **Wrong matches remain possible**: franchise or group codes on websites, same-name companies in
  the same activity. Mitigated, not eliminated; the analyst sees the match method and evidence.
- **Reviews are a noisy proxy for workload** (staff elasticity 0.23); tourist areas and loyal
  customer bases distort it.
- **Legitimate low headcount**: owners/MB members working uninsured, staff on another group
  company's payroll, agencies, service contracts. Shown as caveats, not modelled.
- **Seasonality**: flower shops and tyre services have peaks; the model uses a 3-month maximum and
  12-month average but no seasonal baseline.
- **Sodra hides wages below 3 insured**, so the wage signal never applies to the smallest firms.
- **Small, conservative output**: 3 flagged + 12 watchlist in the real run. Thresholds were set by
  judgement on this run, not calibrated against inspection outcomes.
- **Google data retention**: storing ratings over months and committing a snapshot to a public
  repo is acceptable for this exercise but would need a licensing review in production.
- **Example output names real companies** because the task requires a real run; they are signals,not findings.
