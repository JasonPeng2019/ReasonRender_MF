"""ContextMesh cost dashboard — Streamlit in Snowflake.

Reads the TELEMETRY views (never raw tables), so swapping synthetic -> real proxy
data requires zero changes here. Deploy: snow streamlit deploy --replace -c contextmesh
(from this directory).
"""

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

# Dual-mode: inside Snowflake use the active session; locally fall back to the
# `contextmesh` named connection (key-pair auth from ~/.snowflake).
try:
    from snowflake.snowpark.context import get_active_session
    _SIS_SESSION = get_active_session()
except Exception:
    _SIS_SESSION = None

st.set_page_config(page_title="ContextMesh Cost", layout="wide")

# ---------------------------------------------------------------- palette / chart chrome
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
MUTED = "#898781"
GRID = "#e1e0d9"
BASELINE = "#c3c2b7"
FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif'

ARM_COLORS = {"A0": "#2a78d6", "A": "#eb6834", "B": "#1baf7a"}          # fixed slot order
ROLE_COLORS = {"orchestrator": "#2a78d6", "subagent": "#eb6834", "summarizer": "#1baf7a"}
TOKEN_COLORS = {"Input": "#2a78d6", "Output": "#eb6834",
                "Cache read": "#1baf7a", "Cache write": "#eda100"}
GOOD, CRITICAL = "#0ca30c", "#d03b3b"


def style(fig: go.Figure, ytitle: str = "", xtitle: str = "") -> go.Figure:
    fig.update_layout(
        paper_bgcolor=SURFACE, plot_bgcolor=SURFACE,
        font=dict(family=FONT, color=INK, size=13),
        margin=dict(l=10, r=10, t=10, b=10),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        hovermode="closest",
    )
    fig.update_xaxes(title=xtitle, gridcolor=GRID, linecolor=BASELINE,
                     tickfont=dict(color=MUTED), zeroline=False)
    fig.update_yaxes(title=ytitle, gridcolor=GRID, linecolor=BASELINE,
                     tickfont=dict(color=MUTED), zeroline=False)
    return fig


@st.cache_resource
def _local_conn():
    import snowflake.connector
    return snowflake.connector.connect(connection_name="contextmesh",
                                       warehouse="CONTEXTMESH_WH")


@st.cache_data(ttl=600)
def q(sql: str) -> pd.DataFrame:
    if _SIS_SESSION is not None:
        return _SIS_SESSION.sql(sql).to_pandas()
    return _local_conn().cursor().execute(sql).fetch_pandas_all()


run_cost = q("SELECT * FROM CONTEXTMESH_DB.TELEMETRY.V_RUN_COST")
success_cost = q("SELECT * FROM CONTEXTMESH_DB.TELEMETRY.V_SUCCESS_COST")
redundancy = q("SELECT * FROM CONTEXTMESH_DB.TELEMETRY.V_REDUNDANCY")
hotlist = q("SELECT * FROM CONTEXTMESH_DB.TELEMETRY.V_FILE_HOTLIST")
attribution = q("SELECT * FROM CONTEXTMESH_DB.TELEMETRY.V_SUBAGENT_ATTRIBUTION")
crosscheck = q("SELECT * FROM CONTEXTMESH_DB.TELEMETRY.V_CROSSCHECK")
sources = q("SELECT DISTINCT SOURCE FROM CONTEXTMESH_DB.TELEMETRY.AGENT_TOKEN_EVENTS")

st.title("ContextMesh — agent cost telemetry")
st.caption("A0 = stock, caching stripped · A = stock + native caching (baseline) · "
           "B = ContextMesh plugin (digests + result compression) · cold/warm = memory store filling/full")
if (sources["SOURCE"] == "synthetic").all():
    st.warning("Showing **synthetic placeholder data** — shapes the dashboard until real proxy runs land.")

# ---------------------------------------------------------------- filters
fc1, fc2, _ = st.columns([1, 2, 2])
arm_sel = fc1.multiselect("Arms", ["A0", "A", "B"], default=["A0", "A", "B"])
task_sel = fc2.multiselect("Tasks", sorted(run_cost["TASK_ID"].unique()),
                           default=sorted(run_cost["TASK_ID"].unique()))
rc = run_cost[run_cost["ARM"].isin(arm_sel) & run_cost["TASK_ID"].isin(task_sel)]

# ---------------------------------------------------------------- KPI row
def arm_stat(arm, phase, col):
    row = success_cost[(success_cost["ARM"] == arm) & (success_cost["PHASE"] == phase)]
    return float(row[col].iloc[0]) if len(row) else float("nan")

cost_a = arm_stat("A", "cold", "TOTAL_COST_USD")
cost_b = arm_stat("B", "cold", "TOTAL_COST_USD")
cost_bw = arm_stat("B", "warm", "TOTAL_COST_USD")
overhead = success_cost[success_cost["ARM"] == "B"]["OVERHEAD_COST_USD"].sum()

k = st.columns(5)
k[0].metric("Arm A total (baseline)", f"${cost_a:,.2f}",
            f"success {arm_stat('A', 'cold', 'SUCCESS_RATE'):.0%}", delta_color="off")
k[1].metric("Arm B cold total", f"${cost_b:,.2f}", f"{(cost_b - cost_a) / cost_a:+.1%} vs A",
            delta_color="inverse")
k[2].metric("Arm B warm total", f"${cost_bw:,.2f}", f"{(cost_bw - cost_a) / cost_a:+.1%} vs A",
            delta_color="inverse")
k[3].metric("Summarizer overhead (in B)", f"${overhead:,.2f}", "netted into B totals",
            delta_color="off")
red_b = redundancy[(redundancy["ARM"] == "B") & (redundancy["PHASE"] == "cold")]
k[4].metric("Digest serve / escape rate",
            f"{float(red_b['DIGEST_SERVE_RATE'].iloc[0]):.0%} / {float(red_b['ESCAPE_HATCH_RATE'].iloc[0]):.0%}"
            if len(red_b) else "—", "B cold", delta_color="off")

tab_head, tab_red, tab_attr, tab_meter, tab_data = st.tabs(
    ["Headline", "Redundancy", "Attribution", "Cache & cross-check", "Data"])

# ---------------------------------------------------------------- headline
with tab_head:
    c1, c2 = st.columns(2)

    with c1:
        st.subheader("Cost per task by arm (cold)")
        d = (rc[rc["PHASE"] == "cold"].groupby(["TASK_ID", "ARM"], as_index=False)
             .agg(COST=("TOTAL_COST_USD", "mean")))
        fig = go.Figure()
        for arm in [a for a in ["A0", "A", "B"] if a in arm_sel]:
            da = d[d["ARM"] == arm]
            fig.add_bar(x=da["TASK_ID"], y=da["COST"], name=f"Arm {arm}",
                        marker_color=ARM_COLORS[arm],
                        marker_line=dict(color=SURFACE, width=2))
        fig.update_layout(barmode="group", bargap=0.25)
        st.plotly_chart(style(fig, "avg cost per run (USD)"), use_container_width=True)

    with c2:
        st.subheader("Falling cost as the store fills")
        curve = (run_cost[run_cost["TASK_ID"].isin(task_sel)]
                 .groupby(["TASK_SEQ", "ARM", "PHASE"], as_index=False)
                 .agg(COST=("TOTAL_COST_USD", "mean")))
        fig = go.Figure()
        series = [("A", "cold", "Arm A (stock)", ARM_COLORS["A"], "solid"),
                  ("B", "cold", "Arm B cold — store filling", ARM_COLORS["B"], "solid"),
                  ("B", "warm", "Arm B warm — store full", ARM_COLORS["B"], "dash")]
        for arm, phase, label, color, dash in series:
            dd = curve[(curve["ARM"] == arm) & (curve["PHASE"] == phase)].sort_values("TASK_SEQ")
            if len(dd):
                fig.add_scatter(x=dd["TASK_SEQ"], y=dd["COST"], name=label, mode="lines+markers",
                                line=dict(color=color, width=2, dash=dash),
                                marker=dict(size=8, color=color,
                                            line=dict(color=SURFACE, width=2)))
        st.plotly_chart(style(fig, "avg cost per run (USD)", "task position in suite"),
                        use_container_width=True)

    st.subheader("Success sits next to every cost number")
    d = success_cost.sort_values(["ARM", "PHASE"])
    fig = go.Figure()
    fig.add_bar(x=d["ARM"] + " " + d["PHASE"], y=d["SUCCESS_RATE"],
                marker_color=[GOOD if v >= 0.9 else CRITICAL for v in d["SUCCESS_RATE"]],
                marker_line=dict(color=SURFACE, width=2),
                text=[f"{v:.0%} ({int(s)}/{int(r)})" for v, s, r in
                      zip(d["SUCCESS_RATE"], d["SUCCESSES"], d["RUNS"])],
                textposition="outside", textfont=dict(color=INK))
    fig.update_yaxes(range=[0, 1.15], tickformat=".0%")
    st.plotly_chart(style(fig, "success rate"), use_container_width=True)
    st.caption("Green ≥ 90%, red below — savings only count at comparable success.")

# ---------------------------------------------------------------- redundancy
with tab_red:
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Tokens spent on duplicate reads")
        d = redundancy.sort_values(["ARM", "PHASE"])
        d = d[d["ARM"].isin(arm_sel)]
        fig = go.Figure()
        fig.add_bar(x=d["ARM"] + " " + d["PHASE"], y=d["DUPLICATE_SERVED_TOKENS"],
                    marker_color=[ARM_COLORS[a] for a in d["ARM"]],
                    marker_line=dict(color=SURFACE, width=2),
                    name="duplicate read tokens")
        st.plotly_chart(style(fig, "tokens reaching the model for re-reads"),
                        use_container_width=True)
        st.caption("Sibling subagents re-reading the same files — the waste ContextMesh attacks. "
                   "In B, re-reads are served as ~30% digests.")
    with c2:
        st.subheader("Most re-read files (arm A)")
        d = (hotlist[(hotlist["ARM"] == "A")]
             .sort_values("DUPLICATE_SERVED_TOKENS", ascending=True).tail(8))
        fig = go.Figure(go.Bar(x=d["DUPLICATE_SERVED_TOKENS"], y=d["FILE_PATH"],
                               orientation="h", marker_color="#2a78d6",
                               marker_line=dict(color=SURFACE, width=2)))
        st.plotly_chart(style(fig, "", "duplicate read tokens"), use_container_width=True)

# ---------------------------------------------------------------- attribution
with tab_attr:
    st.subheader("Who spends the tokens")
    d = (attribution[attribution["ARM"].isin(arm_sel) & attribution["TASK_ID"].isin(task_sel)]
         .groupby(["ARM", "PHASE", "AGENT_ROLE"], as_index=False)
         .agg(COST=("COST_USD", "sum")))
    d["ARM_PHASE"] = d["ARM"] + " " + d["PHASE"]
    fig = go.Figure()
    for role in ["orchestrator", "subagent", "summarizer"]:
        dr = d[d["AGENT_ROLE"] == role]
        if len(dr):
            fig.add_bar(x=dr["ARM_PHASE"], y=dr["COST"], name=role,
                        marker_color=ROLE_COLORS[role],
                        marker_line=dict(color=SURFACE, width=2))
    fig.update_layout(barmode="stack", bargap=0.35)
    st.plotly_chart(style(fig, "cost (USD)"), use_container_width=True)
    st.caption("Subagents dominate spend — exactly why sibling redundancy matters. "
               "The summarizer sliver in B is the honestly-counted overhead.")

    st.subheader("Per-session drill-down")
    task_pick = st.selectbox("Task", sorted(attribution["TASK_ID"].unique()))
    d = (attribution[attribution["TASK_ID"] == task_pick]
         .sort_values(["ARM", "PHASE", "TRIAL", "AGENT_ROLE"]))
    st.dataframe(d[["ARM", "PHASE", "TRIAL", "AGENT_ROLE", "SESSION_ID",
                    "CALLS", "TOTAL_TOKENS", "COST_USD"]], use_container_width=True)

# ---------------------------------------------------------------- cache & cross-check
with tab_meter:
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Cache economics")
        d = (rc.groupby(["ARM", "PHASE"], as_index=False)
             .agg(Input=("INPUT_TOKENS", "sum"), Output=("OUTPUT_TOKENS", "sum"),
                  **{"Cache read": ("CACHE_READ_TOKENS", "sum"),
                     "Cache write": ("CACHE_WRITE_TOKENS", "sum")}))
        d["ARM_PHASE"] = d["ARM"] + " " + d["PHASE"]
        fig = go.Figure()
        for cls in ["Input", "Output", "Cache read", "Cache write"]:
            fig.add_bar(x=d["ARM_PHASE"], y=d[cls], name=cls, marker_color=TOKEN_COLORS[cls],
                        marker_line=dict(color=SURFACE, width=2))
        fig.update_layout(barmode="stack", bargap=0.35)
        st.plotly_chart(style(fig, "tokens"), use_container_width=True)
        st.caption("A0 vs A isolates what native caching already gives; B shrinks what enters the cache at all.")
    with c2:
        st.subheader("Two meters agree")
        d = crosscheck[crosscheck["ARM"].isin(arm_sel)]
        lim = float(max(d["NATIVE_TOTAL_TOKENS"].max(), d["PROXY_TOTAL_TOKENS"].max())) * 1.05
        fig = go.Figure()
        fig.add_scatter(x=[0, lim], y=[0, lim], mode="lines", name="perfect agreement",
                        line=dict(color=BASELINE, width=2, dash="dot"), hoverinfo="skip")
        for arm in [a for a in ["A0", "A", "B"] if a in arm_sel]:
            da = d[d["ARM"] == arm]
            fig.add_scatter(x=da["NATIVE_TOTAL_TOKENS"], y=da["PROXY_TOTAL_TOKENS"],
                            mode="markers", name=f"Arm {arm}",
                            marker=dict(size=8, color=ARM_COLORS[arm],
                                        line=dict(color=SURFACE, width=2)))
        st.plotly_chart(style(fig, "proxy-measured tokens", "opencode-native tokens"),
                        use_container_width=True)
        st.caption(f"Per-session totals from two independent meters. "
                   f"Median |diff|: {d['PCT_DIFF'].abs().median():.2f}%")

# ---------------------------------------------------------------- data
with tab_data:
    st.subheader("Arm summary (V_SUCCESS_COST)")
    st.dataframe(success_cost, use_container_width=True)
    st.subheader("Per-run detail (V_RUN_COST)")
    st.dataframe(rc.sort_values(["ARM", "PHASE", "TASK_ID", "TRIAL"]), use_container_width=True)
    st.subheader("Redundancy (V_REDUNDANCY)")
    st.dataframe(redundancy, use_container_width=True)
