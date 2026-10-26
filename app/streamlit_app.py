import duckdb
import pandas as pd
import streamlit as st

from shadow.config import CATEGORIES, DB_PATH, MATCH_THRESHOLD

st.set_page_config(page_title="Shadow-economy leads", layout="wide")
st.title("Shadow-economy leads: Vilnius")
st.caption(
    "Prioritised leads for review, **not accusations**. Each lead lists its evidence and caveats; "
    "check them before acting."
)

if not DB_PATH.exists():
    st.info("No data yet. Run the pipeline: `python -m shadow.pipeline`")
    st.stop()

LABELS = {k: c.label for k, c in CATEGORIES.items()}
FLAGGED_TIERS = ["multiple signals", "staffing gap only"]


def connect() -> duckdb.DuckDBPyConnection:
    # Analyst SQL runs here too: no writes, no file system or network access
    return duckdb.connect(
        str(DB_PATH), read_only=True, config={"enable_external_access": False, "lock_configuration": True}
    )


@st.cache_data(show_spinner=False)
def query(sql: str, params: tuple = (), db_version: float = 0.0) -> pd.DataFrame:
    with connect() as con:
        return con.execute(sql, list(params)).df()


def q(sql: str, *params) -> pd.DataFrame:
    # Re-query when the pipeline rewrites the database
    return query(sql, params, db_version=DB_PATH.stat().st_mtime)


runs = q(
    """
    SELECT r.run_id, r.started_at, r.status, count(l.company_code) AS scored, count(*) FILTER (WHERE l.flagged) AS flagged
    FROM runs r JOIN leads l USING (run_id) GROUP BY ALL ORDER BY r.run_id DESC
    """
)
if runs.empty:
    st.info("The pipeline has not produced any scored run yet.")
    st.stop()

with st.sidebar:
    run_id = st.selectbox("Run", list(runs["run_id"]))
    current = runs[runs["run_id"] == run_id].iloc[0]
    st.caption(f"{current['scored']} companies scored · {current['flagged']} flagged · status {current['status']}")
    dq = q("SELECT * FROM dq_checks WHERE run_id = ? ORDER BY passed, severity, check_name", run_id)
    dq_errors = dq[~dq["passed"] & (dq["severity"] == "error")]
    dq_warnings = dq[~dq["passed"] & (dq["severity"] == "warn")]
    if dq.empty:
        st.warning("No data quality checks for this run.")
    elif len(dq_errors):
        st.error(f"{len(dq_errors)} data quality error(s): do not rely on this run. See Overview.")
    elif len(dq_warnings):
        st.warning(f"{len(dq_warnings)} data quality warning(s). See Overview.")
    else:
        st.success(f"All {len(dq)} data quality checks passed.")
    st.caption(f"Match threshold: {MATCH_THRESHOLD:.2f}")
    with st.expander("How leads are scored"):
        st.markdown(
            "- **Staffing gap**: declared insured vs. what peers with similar review volume declare\n"
            "- **Opening hours**: weekly hours ÷ 40 = minimum full-time staff to keep one person on site\n"
            "- **Low wage**: average wage vs. Vilnius category median (only with ≥ 3 insured)\n\n"
            "**Flagged** = ≥ 2 strong signals, or an extreme staffing gap. **Watchlist** = one strong signal.\n\n"
            "Never flagged: few reviews, bankruptcy/liquidation, company < 12 months old, "
            "listing not confidently matched to a company."
        )

leads_tab, overview_tab, compare_tab, sql_tab = st.tabs(["Leads", "Overview", "Compare runs", "SQL"])

# ---------------------------------------------------------------- Leads
with leads_tab:
    leads = q("SELECT * FROM leads WHERE run_id = ? AND tier IS NOT NULL ORDER BY flagged DESC, score DESC", run_id)
    c1, c2, c3 = st.columns(3)
    cats = c1.multiselect("Category", list(LABELS), default=list(LABELS), format_func=LABELS.get)
    tiers = c2.multiselect("Tier", FLAGGED_TIERS + ["watchlist"], default=FLAGGED_TIERS)
    min_conf = c3.slider("Min. match confidence", MATCH_THRESHOLD, 1.0, MATCH_THRESHOLD, 0.01)
    view = leads[leads["category"].isin(cats) & leads["tier"].isin(tiers) & (leads["match_confidence"] >= min_conf)]

    table = view.assign(category=view["category"].map(LABELS))[
        ["rank", "tier", "score", "company_name", "company_code", "category", "total_reviews",
         "declared_staff", "expected_staff", "weekly_open_hours", "avg_wage", "match_confidence"]
    ]
    st.write(f"**{len(view)}** companies")
    selection = st.dataframe(
        table,
        hide_index=True,
        use_container_width=True,
        on_select="rerun",
        selection_mode="single-row",
        column_config={
            "score": st.column_config.ProgressColumn("score", min_value=0, max_value=100, format="%.0f"),
            "expected_staff": st.column_config.NumberColumn("peer staff", format="%.1f"),
            "weekly_open_hours": st.column_config.NumberColumn("h/week", format="%.0f"),
            "avg_wage": st.column_config.NumberColumn("avg wage €", format="%.0f"),
            "match_confidence": st.column_config.NumberColumn("match conf.", format="%.2f"),
        },
    )
    export = view.copy()
    for col in ("place_ids", "listing_names", "match_methods", "reasons", "caveats"):
        export[col] = export[col].map(lambda v: " | ".join(map(str, v)))
    st.download_button("Download CSV", export.to_csv(index=False), f"leads_{run_id}.csv", "text/csv")

    rows = selection.selection.rows
    if not rows:
        st.info("Select a row to see the evidence behind the lead.")
    else:
        lead = view.iloc[rows[0]]
        st.divider()
        st.subheader(f"{lead['company_name']} · {lead['company_code']}")
        st.caption(
            f"{LABELS[lead['category']]} · {lead['legal_form']} · activity {lead['nace_code']} · "
            f"tier: **{lead['tier']}** · score {lead['score']:.0f}"
        )
        left, right = st.columns([3, 2])
        with left:
            st.markdown("**Why it scored**")
            st.markdown("\n".join(f"- {r}" for r in lead["reasons"]))
            if len(lead["caveats"]):
                st.markdown("**Caveats**")
                st.markdown("\n".join(f"- {c}" for c in lead["caveats"]))
        with right:
            st.markdown("**Signal strength**")
            st.bar_chart(
                pd.DataFrame(
                    {"score": [lead["staffing_score"], lead["hours_score"], lead["wage_score"]]},
                    index=["staffing gap", "opening hours", "low wage"],
                ),
                horizontal=True,
                height=160,
            )

        st.markdown("**Google listings and match evidence**")
        evidence = q(
            """
            SELECT g.name, g.address, g.review_count, g.rating, g.weekly_open_hours, g.maps_url, g.website,
                   m.method, m.confidence, m.reason, m.website_code, m.website_page, m.name_score, g.source, g.fetched_at
            FROM google_listings g JOIN matches m USING (run_id, place_id, category)
            WHERE g.run_id = ? AND g.category = ? AND m.company_code = ? AND m.accepted
            ORDER BY g.review_count DESC
            """,
            run_id, lead["category"], lead["company_code"],
        )
        st.dataframe(
            evidence,
            hide_index=True,
            use_container_width=True,
            column_config={
                "maps_url": st.column_config.LinkColumn("Google Maps", display_text="open"),
                "website": st.column_config.LinkColumn("website"),
                "website_page": st.column_config.LinkColumn("code found on"),
            },
        )

        st.markdown("**Sodra history** (insured persons and average wage)")
        history = q(
            "SELECT month, insured, avg_wage FROM sodra_monthly WHERE company_code = ? ORDER BY month",
            lead["company_code"],
        )
        if history.empty:
            st.warning("No Sodra records for this company.")
        else:
            h1, h2 = st.columns(2)
            h1.line_chart(history.set_index("month")["insured"], height=200)
            h2.line_chart(history.set_index("month")["avg_wage"], height=200)

# ---------------------------------------------------------------- Overview
with overview_tab:
    by_cat = q(
        """
        WITH l AS (SELECT category, count(*) AS listings, count(*) FILTER (WHERE m.accepted) AS matched
                   FROM google_listings g JOIN matches m USING (run_id, place_id, category)
                   WHERE g.run_id = ? GROUP BY 1),
             c AS (SELECT category, count(*) AS companies,
                          count(*) FILTER (WHERE exclusion_reason IS NULL) AS eligible,
                          count(*) FILTER (WHERE tier = 'watchlist') AS watchlist,
                          count(*) FILTER (WHERE flagged) AS flagged
                   FROM leads WHERE run_id = ? GROUP BY 1)
        SELECT l.category, listings, matched, round(matched / listings, 2) AS match_rate,
               companies, eligible, watchlist, flagged, round(flagged / nullif(eligible, 0), 3) AS flagged_share
        FROM l JOIN c USING (category) ORDER BY flagged_share DESC
        """,
        run_id, run_id,
    )
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Google listings", int(by_cat["listings"].sum()))
    m2.metric("Matched to a company", int(by_cat["matched"].sum()))
    m3.metric("Eligible companies", int(by_cat["eligible"].sum()))
    m4.metric("Flagged", int(by_cat["flagged"].sum()))
    st.dataframe(by_cat.assign(category=by_cat["category"].map(LABELS)), hide_index=True, use_container_width=True)

    o1, o2 = st.columns(2)
    with o1:
        st.markdown("**Why companies were not eligible**")
        st.dataframe(
            q(
                """SELECT regexp_replace(exclusion_reason, ' \\(.*\\)', '') AS reason, count(*) AS companies
                   FROM leads WHERE run_id = ? AND exclusion_reason IS NOT NULL GROUP BY 1 ORDER BY 2 DESC""",
                run_id,
            ),
            hide_index=True, use_container_width=True,
        )
        st.markdown("**Why listings were not matched**")
        st.dataframe(
            q(
                """SELECT method, reason, count(*) AS listings FROM matches
                   WHERE run_id = ? AND NOT accepted GROUP BY ALL ORDER BY 3 DESC LIMIT 10""",
                run_id,
            ),
            hide_index=True, use_container_width=True,
        )
    with o2:
        st.markdown("**Data freshness**")
        st.dataframe(
            q(
                """SELECT 'Google listings' AS source, max(fetched_at)::VARCHAR AS as_of, any_value(source) AS origin
                   FROM google_listings WHERE run_id = ?
                   UNION ALL SELECT 'Sodra', max(month)::VARCHAR, 'atvira.sodra.lt' FROM sodra_monthly
                   UNION ALL SELECT 'Registrų centras (JAR)', max(loaded_at)::VARCHAR, 'get.data.gov.lt' FROM registry_entities""",
                run_id,
            ),
            hide_index=True, use_container_width=True,
        )
        st.markdown("**Flagged and watchlist locations**")
        points = q(
            """WITH p AS (SELECT run_id, category, unnest(place_ids) AS place_id
                         FROM leads WHERE run_id = ? AND tier IS NOT NULL)
               SELECT g.lat, g.lon FROM p JOIN google_listings g USING (run_id, place_id, category)""",
            run_id,
        )
        if not points.empty:
            st.map(points, latitude="lat", longitude="lon", size=40)

    st.markdown("**Data quality checks**")
    if dq.empty:
        st.info("Run the `quality` step to populate checks.")
    else:
        st.dataframe(
            dq.assign(result=dq.apply(lambda r: "pass" if r["passed"] else f"FAIL ({r['severity']})", axis=1))[
                ["result", "check_name", "value", "threshold", "details"]
            ],
            hide_index=True,
            use_container_width=True,
        )

# ---------------------------------------------------------------- Compare runs
with compare_tab:
    if len(runs) < 2:
        st.info("Only one scored run so far. Run the pipeline again next month to compare results.")
    else:
        others = [r for r in runs["run_id"] if r != run_id]
        previous = st.selectbox("Compare with", others)
        diff = q(
            """
            WITH cur AS (SELECT * FROM leads WHERE run_id = ? AND flagged),
                 prev AS (SELECT * FROM leads WHERE run_id = ? AND flagged)
            SELECT CASE WHEN p.company_code IS NULL THEN 'new'
                        WHEN c.company_code IS NULL THEN 'dropped' ELSE 'persisting' END AS change,
                   coalesce(c.company_name, p.company_name) AS company_name,
                   coalesce(c.company_code, p.company_code) AS company_code,
                   coalesce(c.category, p.category) AS category,
                   p.score AS previous_score, c.score AS current_score,
                   p.declared_staff AS previous_staff, c.declared_staff AS current_staff,
                   c.review_delta
            FROM cur c FULL JOIN prev p USING (company_code, category)
            ORDER BY change, current_score DESC NULLS LAST
            """,
            run_id, previous,
        )
        k1, k2, k3 = st.columns(3)
        for col, label in zip((k1, k2, k3), ("new", "persisting", "dropped")):
            col.metric(label.capitalize(), int((diff["change"] == label).sum()))
        st.dataframe(diff.assign(category=diff["category"].map(LABELS)), hide_index=True, use_container_width=True)
        st.caption("Dropped leads may have hired staff, changed matching, or fallen below a threshold; check the Leads tab of each run.")

# ---------------------------------------------------------------- SQL
EXAMPLES = {
    "Flagged share by category": """SELECT category,
       count(*) FILTER (WHERE flagged) AS n_flagged,
       count(*) FILTER (WHERE exclusion_reason IS NULL) AS n_eligible,
       round(n_flagged / nullif(n_eligible, 0), 3) AS flagged_share
FROM leads WHERE run_id = (SELECT max(run_id) FROM leads)
GROUP BY 1 ORDER BY flagged_share DESC""",
    "Match methods": """SELECT category, method, count(*) AS listings, round(avg(confidence), 2) AS avg_conf
FROM matches WHERE run_id = (SELECT max(run_id) FROM matches)
GROUP BY ALL ORDER BY 1, 3 DESC""",
    "Busiest listings with ≤ 2 declared staff": """SELECT company_name, category, total_reviews, declared_staff, tier, exclusion_reason
FROM leads WHERE run_id = (SELECT max(run_id) FROM leads) AND declared_staff <= 2
ORDER BY total_reviews DESC LIMIT 20""",
    "Data quality history": """SELECT run_id, count(*) AS checks,
       count(*) FILTER (WHERE NOT passed AND severity = 'error') AS errors,
       count(*) FILTER (WHERE NOT passed AND severity = 'warn') AS warnings
FROM dq_checks GROUP BY 1 ORDER BY 1 DESC""",
    "Tables and columns": "SELECT table_name, column_name, data_type FROM information_schema.columns ORDER BY 1, ordinal_position",
}
with sql_tab:
    st.caption(
        "Read-only access to all tables: `leads`, `matches`, `google_listings`, `sodra_monthly`, "
        "`registry_entities`, `runs`, `dq_checks`."
    )
    example = st.selectbox("Example", list(EXAMPLES))
    sql = st.text_area("SQL", EXAMPLES[example], height=160)
    if st.button("Run", type="primary"):
        try:
            with connect() as con:
                result = con.execute(sql).df()
            st.write(f"{len(result)} rows")
            st.dataframe(result, hide_index=True, use_container_width=True)
        except duckdb.Error as exc:
            st.error(str(exc))
