"""Streamlit visual QA dashboard for the three onion forecasting graphs.

This dashboard is for model/response inspection. The production frontend team can
consume the same FastAPI endpoints; see FRONTEND_INTEGRATION.md.
"""
from __future__ import annotations
from datetime import date, timedelta
import os
from typing import Any

import pandas as pd
import plotly.graph_objects as go
from plotly.colors import qualitative
import requests
import streamlit as st
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(ROOT, ".env"))

st.set_page_config(page_title="AP Civil Supplies · Onion Outlook", page_icon="🧅", layout="wide")

# Align the local visual QA tool with the supplied AP Civil Supplies frontend reference.
st.markdown("""
<style>
  .block-container { padding-top: 1.4rem; padding-bottom: 3rem; max-width: 1500px; }
  .app-kicker { color:#657080; font-size:.79rem; letter-spacing:.07em; text-transform:uppercase; }
  .hero { border:1px solid #e4e8ef; border-radius:18px; padding:20px 24px; background:linear-gradient(115deg,#ffffff 0%,#f8fafc 100%); margin: 0.4rem 0 1.5rem; }
  .hero-title { font-size:1.7rem; font-weight:750; color:#161c28; margin:0; }
  .hero-sub { color:#687385; margin-top:4px; font-size:.94rem; }
  .section-title { font-size:1.55rem; font-weight:750; letter-spacing:-.025em; color:#171c27; margin: .5rem 0 0; }
  .section-subtitle { color:#697586; font-size:.91rem; margin:.15rem 0 .8rem; }
  .section-rule { border:0; border-top:1px solid #edf0f4; margin:1.6rem 0; }
  div[data-testid="stMetric"] { border:1px solid #e9edf2; border-radius:12px; padding:13px 15px; background:#fff; }
  div[data-testid="stMetricLabel"] { color:#647083; }
  .data-note { font-size:.82rem; color:#697586; }
  div.stButton > button[kind="primary"] { border-radius:10px; }
</style>
""", unsafe_allow_html=True)

STATE_LABELS = {
    "AP": "Andhra Pradesh",
    "KA": "Karnataka",
    "MH": "Maharashtra",
    "MP": "Madhya Pradesh",
}
HORIZONS = [7, 14, 30, 60, 90, 180]
HORIZON_LABELS = {7: "1W", 14: "2W", 30: "1M", 60: "2M", 90: "3M", 180: "6M"}
STATE_COLORS = {"AP": "#4469a8", "KA": "#bd6b88", "MH": "#9270b5", "MP": "#599a91"}
PRICE_COLORS = {"retail": "#4469a8", "mandi_modal": "#b28a3b", "wholesale": "#b28a3b"}

with st.sidebar:
    st.header("Connection")
    api_base = st.text_input(
        "FastAPI base URL", value=os.getenv("API_BASE_URL", "http://127.0.0.1:8000")
    ).strip().rstrip("/")
    st.markdown(f"[Open API docs]({api_base}/docs)")
    if st.button("Check API health", use_container_width=True):
        try:
            response = requests.get(f"{api_base}/health", timeout=10)
            response.raise_for_status()
            health = response.json()
            st.success(f"API running · latest data {health.get('last_observation_date', 'unknown')}")
            st.json({k: health.get(k) for k in ("model_loaded", "last_observation_date", "data_staleness_days", "ingestion")})
        except Exception as exc:
            st.error(f"Could not reach API: {exc}")
    st.divider()
    st.caption("P50 is the median forecast. P10–P90 is a nominal 80% prediction interval, not a calibrated confidence score.")


@st.cache_data(ttl=300, show_spinner=False)
def api_get(url: str, params: tuple[tuple[str, Any], ...]) -> dict:
    token = os.getenv("API_BEARER_TOKEN", "").strip()
    if not token:
        raise RuntimeError("API_BEARER_TOKEN is missing from the dashboard environment.")

    response = requests.get(
        url,
        params=dict(params),
        headers={"Authorization": f"Bearer {token}"},
        timeout=1800,
    )
    try:
        body = response.json()
    except ValueError:
        body = {"detail": response.text[:1000]}
    if not response.ok:
        raise RuntimeError(body.get("detail", f"API HTTP {response.status_code}"))
    return body


def fetch_graph(endpoint: str, params: dict[str, Any]) -> dict:
    return api_get(f"{api_base}{endpoint}", tuple(sorted(params.items())))


def forecast_hover_data(forecast: pd.DataFrame) -> tuple[Any, str]:
    fields = [
        "latest_actual_price_rs_kg",
        "change_rs_kg",
        "change_percent",
        "change_direction",
        "confidence_score",
    ]
    if not all(field in forecast.columns for field in fields):
        return None, "Date: %{x}<br>P50: ₹%{y:.2f}/kg<extra></extra>"

    return forecast[fields].to_numpy(), (
        "Date: %{x}<br>P50: ₹%{y:.2f}/kg"
        "<br>Latest actual: ₹%{customdata[0]:.2f}/kg"
        "<br>Change: %{customdata[1]:+.2f} ₹/kg"
        "<br>Change: %{customdata[2]:+.2f}% (%{customdata[3]})"
        "<br>Confidence: %{customdata[4]:.1f}%"
        "<extra></extra>"
    )


def set_freshness(payload: dict) -> None:
    last_date = payload.get("last_observation_date")
    if last_date:
        st.caption(
            f"Latest actual data: {last_date}  ·  Forecast: "
            f"{payload.get('forecast_start_date')} → {payload.get('forecast_end_date')}  ·  "
            f"Model context rows: {payload.get('model_history_rows', 'full stored history')}"
        )
    for warning in payload.get("warnings", []):
        st.warning(warning)


def section_heading(title: str, subtitle: str) -> None:
    st.markdown(f'<div class="section-title">{title}</div><div class="section-subtitle">{subtitle}</div>', unsafe_allow_html=True)


def date_range_control(
    key: str,
    label: str,
    start_days_before: int = 30,
    end_days_after: int = 30,
) -> tuple[date, date] | None:
    selected = st.date_input(
        label,
        value=(
            date.today() - timedelta(days=start_days_before),
            date.today() + timedelta(days=end_days_after),
        ),
        key=key,
    )
    if not isinstance(selected, tuple) or len(selected) != 2:
        st.info("Select both a start date and an end date.")
        return None
    return selected


def add_forecast_band(fig: go.Figure, x, lower, upper, color: str, name: str, legendgroup: str) -> None:
    import re
    m = re.match(r"#([0-9a-fA-F]{6})", color)
    if m:
        value = m.group(1)
        r, g, b = (int(value[i:i + 2], 16) for i in (0, 2, 4))
        fill = f"rgba({r},{g},{b},0.14)"
    else:
        fill = "rgba(68,105,168,0.14)"
    fig.add_trace(go.Scatter(
        x=x, y=upper, mode="lines", line={"width": 0, "color": color},
        showlegend=False, hoverinfo="skip", legendgroup=legendgroup,
    ))
    fig.add_trace(go.Scatter(
        x=x, y=lower, mode="lines", line={"width": 0, "color": color},
        fill="tonexty", fillcolor=fill, name=name, legendgroup=legendgroup,
        hovertemplate="Date: %{x}<br>Lower bound: ₹%{y:.2f}/kg<extra></extra>",
    ))


def configure_chart(fig: go.Figure, y_title: str = "Price (₹/kg)", height: int = 420) -> None:
    fig.update_layout(
        height=height, margin={"l": 12, "r": 18, "t": 18, "b": 15},
        paper_bgcolor="white", plot_bgcolor="white", hovermode="x unified",
        xaxis_title="", yaxis_title=y_title,
        font={"family": "Inter, Arial, sans-serif", "color": "#394252"},
        xaxis={"gridcolor": "#f0f2f5", "linecolor": "#e6eaf0", "zeroline": False},
        yaxis={"gridcolor": "#e7ebf1", "linecolor": "#e6eaf0", "zeroline": False},
        legend={"orientation": "h", "y": -0.22, "x": 0},
    )


# Header and summary card
st.markdown('<div class="app-kicker">AP Civil Supplies · AI price outlook · Andhra Pradesh</div>', unsafe_allow_html=True)
st.markdown('''<div class="hero">
  <div class="app-kicker">Vegetable · Onion</div>
  <div class="hero-title">Onion price outlook</div>
  <div class="hero-sub">Chronos-2 forecasting · Retail, wholesale and mandi modal prices · State comparison · Supply-demand outlook</div>
</div>''', unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Graph 1 — AP price outlook. Forecast is calculated from all stored rows;
# API only returns 90 days of actuals for the chart.
# ---------------------------------------------------------------------------
section_heading("AP retail price forecast", "Andhra Pradesh onion prices · Last 3 months of actuals and the selected forecast horizon")
c1, c2 = st.columns([1.35, 1.2])
with c1:
    range1 = date_range_control("g1_date_range", "Date range")
with c2:
    opt1, opt2, opt3 = st.columns(3)
    with opt1:
        show_retail = st.checkbox("Retail price", value=True, key="g1_retail")
    with opt2:
        show_wholesale = st.checkbox("Wholesale price", value=False, key="g1_wholesale")
    with opt3:
        show_mandi = st.checkbox("Mandi modal price", value=False, key="g1_mandi")
run1 = st.button("Update AP forecast", type="primary", key="g1_run")
if run1:
    api_get.clear()
    st.session_state["g1_started"] = True
if run1 or st.session_state.get("g1_started", False):
    selected_price_types = []
    if show_retail:
        selected_price_types.append("retail")
    if show_wholesale:
        selected_price_types.append("wholesale")
    if show_mandi:
        selected_price_types.append("mandi_modal")
    if not selected_price_types:
        st.session_state["g1_error"] = "Select retail, wholesale, and/or mandi modal price."
        st.session_state.pop("g1_payload", None)
    else:
        with st.spinner("Running Chronos-2 on the full available historical context..."):
            try:
                st.session_state["g1_payload"] = fetch_graph(
                    "/api/graphs/ap-retail",
                    {
    "price_types": ",".join(selected_price_types),
    "start_date": range1[0].isoformat(),
    "end_date": range1[1].isoformat(),
},
                )
                st.session_state["g1_error"] = None
            except Exception as exc:
                st.session_state["g1_error"] = str(exc)
if st.session_state.get("g1_error"):
    st.error(st.session_state["g1_error"])
payload1 = st.session_state.get("g1_payload")
if payload1:
    set_freshness(payload1)
    hdf = pd.DataFrame(payload1.get("historical", []))
    fdf = pd.DataFrame(payload1.get("forecast", []))
    if not hdf.empty and not fdf.empty:
        fig1 = go.Figure()
        for ptype in payload1.get("price_types", []):
            color = PRICE_COLORS.get(ptype, PRICE_COLORS["wholesale"])
            label = {"retail": "Retail price", "wholesale": "Wholesale price", "mandi_modal": "Mandi modal price"}.get(ptype, ptype)
            hist = hdf[hdf["price_type"] == ptype]
            pred = fdf[fdf["price_type"] == ptype]
            if not hist.empty:
                fig1.add_trace(go.Scatter(
                    x=hist["date"], y=hist["actual_price_rs_kg"], mode="lines",
                    name=f"{label} · actual", line={"color": color, "width": 2.3},
                    legendgroup=ptype,
                ))
            if not pred.empty:
                add_forecast_band(fig1, pred["date"], pred["p10_rs_kg"], pred["p90_rs_kg"], color, f"{label} · P10–P90", ptype)
                hover_data, hover_template = forecast_hover_data(pred)
                fig1.add_trace(go.Scatter(
                    x=pred["date"], y=pred["predicted_price_rs_kg"], mode="lines",
                    customdata=hover_data,
                    name=f"{label} · forecast P50", line={"color": color, "width": 2.7, "dash": "dash"},
                    legendgroup=ptype,
                    hovertemplate=hover_template,
                ))
        threshold1 = payload1.get("threshold_rs_kg", 44.0)
        fig1.add_hline(
            y=threshold1,
            line_dash="dash",
            line_color="#d9534f",
            annotation_text=f"Threshold ₹{threshold1:g}/kg",
            annotation_position="top left",
        )
        # Mark the boundary between latest actual and future forecast.
        fig1.add_vline(x=payload1.get("forecast_start_date"), line_dash="dot", line_color="#8d96a5", opacity=.7)
        configure_chart(fig1, height=420)
        st.plotly_chart(fig1, use_container_width=True)

        # Compare the first forecast with the latest actual returned by the API.
        metrics = []
        for ptype in payload1.get("price_types", []):
            pred = fdf[fdf["price_type"] == ptype]
            if not pred.empty:
                label = {"retail": "AP retail", "wholesale": "AP wholesale", "mandi_modal": "AP mandi modal"}.get(ptype, ptype)
                metrics.append((label, pred.sort_values("date").iloc[0]))
        if metrics:
            cols = st.columns(len(metrics))
            for col, (label, forecast_row) in zip(cols, metrics):
                actual = forecast_row.get("latest_actual_price_rs_kg")
                actual_date = forecast_row.get("latest_actual_date")
                change_percent = forecast_row.get("change_percent")
                direction = forecast_row.get("change_direction")
                delta = (
                    f"{change_percent:+.2f}% · {direction}"
                    if pd.notna(change_percent) and direction
                    else "Change fields missing from API"
                )
                col.metric(
                    f"{label} · latest actual",
                    f"₹{actual:.2f}/kg" if pd.notna(actual) else "Unavailable",
                    delta=delta,
                )
                confidence = forecast_row.get("confidence_score")
                confidence_text = (
                    f" · Confidence {confidence:.1f}%"
                    if confidence is not None and pd.notna(confidence)
                    else ""
                )
                col.caption(
                    f"Baseline {actual_date or 'unavailable'} · First forecast "
                    f"₹{forecast_row['predicted_price_rs_kg']:.2f}/kg "
                    f"({forecast_row['date']}) · P10 ₹{forecast_row['p10_rs_kg']:.2f} "
                    f"· P90 ₹{forecast_row['p90_rs_kg']:.2f}{confidence_text}"
                )
        with st.expander("Forecast rows / API values"):
            st.dataframe(fdf, use_container_width=True, hide_index=True)
        st.download_button("Download AP price forecast CSV", fdf.to_csv(index=False), file_name=f"ap_price_forecast_{payload1['horizon_days']}d.csv", mime="text/csv", key="g1_download")
else:
    st.info("Choose any combination of retail, wholesale and mandi modal prices, select a horizon, then click Update AP forecast.")

st.markdown('<hr class="section-rule"/>', unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Graph 2 — selected states only, with retail and wholesale/mandi prices.
# ---------------------------------------------------------------------------
section_heading("AP vs other states", "Compare only the states you select · Retail, wholesale and mandi modal prices")
g2a, g2b = st.columns([1.5, 1.25])
with g2a:
    selected_states = st.multiselect(
        "States to compare", options=list(STATE_LABELS), default=["AP", "KA", "MH", "MP"],
        format_func=lambda code: STATE_LABELS[code], key="g2_states",
    )
with g2b:
    selected_price_types2 = st.multiselect("Price types", ["retail", "wholesale", "mandi_modal"], default=["retail", "wholesale", "mandi_modal"], format_func=lambda x: {"retail": "Retail price", "wholesale": "Wholesale price", "mandi_modal": "Mandi modal price"}[x], key="g2_types")
range2 = date_range_control("g2_date_range", "Date range")
run2 = st.button("Update state comparison", type="primary", key="g2_run")
if run2:
    api_get.clear()
    st.session_state["g2_started"] = True
if run2 or st.session_state.get("g2_started", False):
    if not selected_states or not selected_price_types2:
        st.session_state["g2_error"] = "Select at least one state and one price type."
        st.session_state.pop("g2_payload", None)
    else:
        with st.spinner("Forecasting selected state and price series..."):
            try:
                st.session_state["g2_payload"] = fetch_graph(
                    "/api/graphs/state-comparison",
                    {
    "states": ",".join(selected_states),
    "price_types": ",".join(selected_price_types2),
    "start_date": range2[0].isoformat(),
    "end_date": range2[1].isoformat(),
},
                )
                st.session_state["g2_error"] = None
            except Exception as exc:
                st.session_state["g2_error"] = str(exc)
if st.session_state.get("g2_error"):
    st.error(st.session_state["g2_error"])
payload2 = st.session_state.get("g2_payload")
if payload2:
    set_freshness(payload2)
    hdf2 = pd.DataFrame(payload2.get("historical", []))
    fdf2 = pd.DataFrame(payload2.get("forecast", []))
    fig2 = go.Figure()
    for state in payload2.get("selected_states", []):
        color = STATE_COLORS.get(state, qualitative.Plotly[0])
        for ptype in payload2.get("selected_price_types", []):
            label = f"{STATE_LABELS.get(state, state)} · {dict(retail='Retail', wholesale='Wholesale', mandi_modal='Mandi modal')[ptype]}"
            hist = hdf2[(hdf2["state"] == state) & (hdf2["price_type"] == ptype)] if not hdf2.empty else pd.DataFrame()
            pred = fdf2[(fdf2["state"] == state) & (fdf2["price_type"] == ptype)] if not fdf2.empty else pd.DataFrame()
            dash = {"retail": "solid", "wholesale": "dashdot", "mandi_modal": "dot"}[ptype]
            if not hist.empty:
                fig2.add_trace(go.Scatter(x=hist["date"], y=hist["actual_price_rs_kg"], mode="lines", name=f"{label} · actual", line={"color": color, "width": 2, "dash": dash}, legendgroup=f"{state}-{ptype}"))
            if not pred.empty:
                add_forecast_band(fig2, pred["date"], pred["p10_rs_kg"], pred["p90_rs_kg"], color, f"{label} · P10–P90", f"{state}-{ptype}")
                hover_data, hover_template = forecast_hover_data(pred)
                fig2.add_trace(go.Scatter(
                    x=pred["date"],
                    y=pred["predicted_price_rs_kg"],
                    mode="lines",
                    customdata=hover_data,
                    name=f"{label} · forecast P50",
                    line={"color": color, "width": 2.4, "dash": "dash" if ptype == "retail" else "dashdot"},
                    legendgroup=f"{state}-{ptype}",
                    hovertemplate=hover_template,
                ))
    if payload2.get("forecast_start_date"):
        fig2.add_vline(x=payload2["forecast_start_date"], line_dash="dot", line_color="#8d96a5", opacity=.7)
    threshold2 = payload2.get("threshold_rs_kg", 44.0)
    fig2.add_hline(
        y=threshold2,
        line_dash="dash",
        line_color="#d9534f",
        annotation_text=f"Threshold ₹{threshold2:g}/kg",
        annotation_position="top left",
    )
    configure_chart(fig2, height=470)
    st.plotly_chart(fig2, use_container_width=True)
    st.caption("Only selected states and price types are included in the API response. Forecasts use all stored historical rows; the chart displays the last 90 actual days.")
    if not fdf2.empty:
        comparison_fields = [
            "latest_actual_date",
            "latest_actual_price_rs_kg",
            "change_rs_kg",
            "change_percent",
            "change_direction",
            "confidence_score",
        ]
        missing_fields = [field for field in comparison_fields if field not in fdf2.columns]
        if missing_fields:
            st.warning(
                "The API response is missing comparison fields: "
                + ", ".join(missing_fields)
            )
        else:
            first_forecasts = (
                fdf2.sort_values("date")
                .groupby(["state", "price_type"], as_index=False)
                .first()
            )
            st.markdown("#### First forecast change vs latest actual")
            st.dataframe(
                first_forecasts[
                    [
                        "state",
                        "price_type",
                        "latest_actual_date",
                        "latest_actual_price_rs_kg",
                        "date",
                        "predicted_price_rs_kg",
                        "change_rs_kg",
                        "change_percent",
                        "change_direction",
                        "confidence_score",
                    ]
                ],
                use_container_width=True,
                hide_index=True,
            )
        with st.expander("Comparison forecast rows"):
            st.dataframe(fdf2, use_container_width=True, hide_index=True)
        st.download_button("Download comparison forecast CSV", fdf2.to_csv(index=False), file_name="selected_state_price_forecasts.csv", mime="text/csv", key="g2_download")
else:
    st.info("Select states and price types, choose a horizon and click Update state comparison.")

st.markdown('<hr class="section-rule"/>', unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Graph 3 — forecast monthly AP production, consumption and arrivals, then derive shortfall.
# ---------------------------------------------------------------------------
section_heading("AP monthly supply-demand balance", "Monthly production, consumption and arrivals forecasts · Shortfall = Consumption − (Arrival + Production)")
st.caption("Monthly Andhra Pradesh quantities are shown in tonnes/month. Positive shortfall values indicate potential demand exceeding supply.")
range3 = date_range_control(
    "g3_monthly_date_range",
    "Monthly date range",
    start_days_before=365,
    end_days_after=180,
)
run3 = st.button("Update supply / demand outlook", type="primary", key="g3_run")
if run3:
    st.session_state["g3_started"] = True
if run3 or st.session_state.get("g3_started", False):
    with st.spinner("Forecasting monthly AP production, consumption and arrivals with Chronos-2..."):
        try:
            st.session_state["g3_payload"] = fetch_graph(
                "/api/graphs/supply-balance",
                {
                    "states": "AP",
                    "start_date": range3[0].isoformat(),
                    "end_date": range3[1].isoformat(),
                },
            )
            st.session_state["g3_error"] = None
        except Exception as exc:
            st.session_state["g3_error"] = str(exc)
if st.session_state.get("g3_error"):
    st.error(st.session_state["g3_error"])
payload3 = st.session_state.get("g3_payload")
if payload3:
    set_freshness(payload3)
    qhist = pd.DataFrame(payload3.get("historical", []))
    qpred = pd.DataFrame(payload3.get("quantity_forecast", []))
    bpred = pd.DataFrame(payload3.get("balance_forecast", []))
    if not qhist.empty and not qpred.empty:
        latest_actuals = (
            qhist.assign(_date=pd.to_datetime(qhist["date"]))
            .sort_values("_date")
            .groupby("metric", as_index=False)
            .tail(1)
            .set_index("metric")
        )
        latest_balance = bpred.loc[pd.to_datetime(bpred["date"]).idxmax()] if not bpred.empty else None
        metric_columns = st.columns(4)
        for column, metric, label in zip(
            metric_columns,
            ("production", "consumption", "arrival"),
            ("Latest production", "Latest consumption", "Latest arrivals"),
        ):
            if metric in latest_actuals.index:
                row = latest_actuals.loc[metric]
                column.metric(
                    label,
                    f"{row['actual_quantity_tonnes_month']:,.0f} t",
                    help=f"Latest actual month: {pd.to_datetime(row['date']).strftime('%B %Y')}",
                )
        if latest_balance is not None:
            shortfall = float(latest_balance["shortfall_p50_tonnes_month"])
            metric_columns[3].metric(
                "Forecast shortfall / surplus",
                f"{shortfall:,.0f} t",
                "Potential shortfall" if shortfall > 0 else "Potential surplus",
                delta_color="inverse" if shortfall > 0 else "normal",
                help=f"Forecast month: {pd.to_datetime(latest_balance['date']).strftime('%B %Y')}",
            )

        st.markdown("#### Monthly quantities")
        st.caption("Circles show actuals · Diamonds show forecast medians · Shaded areas show P10–P90 forecast ranges")
        fig3 = go.Figure()
        metric_styles = {
            "production": {"label": "Production", "color": "#18846B", "fill": "rgba(24,132,107,0.13)"},
            "consumption": {"label": "Consumption", "color": "#4469A8", "fill": "rgba(68,105,168,0.13)"},
            "arrival": {"label": "Arrivals", "color": "#C78422", "fill": "rgba(199,132,34,0.16)"},
        }
        for state in payload3.get("selected_states", []):
            hist_state = qhist[qhist["state"] == state]
            pred_state = qpred[qpred["state"] == state]
            for metric, style in metric_styles.items():
                h = hist_state[hist_state["metric"] == metric].sort_values("date")
                f = pred_state[pred_state["metric"] == metric].sort_values("date")
                if not h.empty:
                    fig3.add_trace(go.Scatter(
                        x=h["date"],
                        y=h["actual_quantity_tonnes_month"],
                        mode="lines+markers",
                        name=f"{style['label']} · actual",
                        line={"color": style["color"], "width": 2.3},
                        marker={"color": style["color"], "size": 7, "symbol": "circle"},
                        legendgroup=f"{state}-{metric}",
                        hovertemplate="%{x|%b %Y}<br>Actual: %{y:,.0f} tonnes/month<extra></extra>",
                    ))
                if not f.empty:
                    fig3.add_trace(go.Scatter(
                        x=f["date"],
                        y=f["p90_tonnes_month"],
                        mode="lines",
                        line={"color": style["color"], "width": 0},
                        showlegend=False,
                        hoverinfo="skip",
                        legendgroup=f"{state}-{metric}",
                    ))
                    fig3.add_trace(go.Scatter(
                        x=f["date"],
                        y=f["p10_tonnes_month"],
                        mode="lines",
                        line={"color": style["color"], "width": 0},
                        fill="tonexty",
                        fillcolor=style["fill"],
                        showlegend=False,
                        hoverinfo="skip",
                        legendgroup=f"{state}-{metric}",
                    ))
                    fig3.add_trace(go.Scatter(
                        x=f["date"],
                        y=f["predicted_quantity_tonnes_month"],
                        mode="lines+markers",
                        name=f"{style['label']} · forecast P50",
                        line={"color": style["color"], "width": 2.6, "dash": "dash"},
                        marker={"color": style["color"], "size": 9, "symbol": "diamond"},
                        legendgroup=f"{state}-{metric}",
                        hovertemplate="%{x|%b %Y}<br>Forecast P50: %{y:,.0f} tonnes/month<extra></extra>",
                    ))
                    if not h.empty:
                        fig3.add_trace(go.Scatter(
                            x=[pd.to_datetime(h["date"].iloc[-1]), pd.to_datetime(f["date"].iloc[0])],
                            y=[
                                h["actual_quantity_tonnes_month"].iloc[-1],
                                f["predicted_quantity_tonnes_month"].iloc[0],
                            ],
                            mode="lines",
                            line={"color": style["color"], "width": 2.6, "dash": "dash"},
                            showlegend=False,
                            hoverinfo="skip",
                            legendgroup=f"{state}-{metric}",
                        ))
        if payload3.get("forecast_start_date"):
            fig3.add_vline(
                x=payload3["forecast_start_date"],
                line_dash="dot",
                line_color="#8d96a5",
                opacity=.8,
                annotation_text="Forecast starts",
                annotation_position="top",
                annotation_font_color="#697586",
            )
        configure_chart(fig3, y_title="Tonnes per month", height=470)
        month_dates = pd.to_datetime(
            pd.concat([qhist["date"], qpred["date"]], ignore_index=True)
        ).drop_duplicates().sort_values()
        fig3.update_xaxes(
            tickmode="array",
            tickvals=month_dates,
            ticktext=[month.strftime("%b %Y") for month in month_dates],
            tickangle=-35,
            automargin=True,
        )
        fig3.update_yaxes(tickformat=",.0f")
        fig3.update_layout(margin={"l": 12, "r": 18, "t": 42, "b": 105}, legend={"orientation": "h", "y": -0.3, "x": 0})
        st.plotly_chart(fig3, use_container_width=True)
        st.caption("Shortfall = Consumption − (Arrival + Production). Positive values indicate potential shortfall; negative values indicate potential surplus.")
    if not bpred.empty:
        latest = bpred[bpred["date"] == bpred["date"].max()]
        cards = st.columns(max(1, len(latest)))
        for col, (_, row) in zip(cards, latest.iterrows()):
            val = float(row["shortfall_p50_tonnes_month"])
            col.metric("Indicative shortfall / surplus · " + row["state"], f"{val:,.1f} tonnes/month", delta="Potential shortfall" if val > 0 else "Potential surplus", delta_color="inverse" if val > 0 else "normal")
        with st.expander("Supply, consumption and shortfall forecast rows"):
            st.dataframe(qpred, use_container_width=True, hide_index=True)
            st.dataframe(bpred, use_container_width=True, hide_index=True)
            for warning in payload3.get("warnings", []):
                st.write(f"- {warning}")
    if not qpred.empty:
        st.download_button("Download supply / consumption forecast CSV", qpred.to_csv(index=False), file_name="supply_consumption_forecasts.csv", mime="text/csv", key="g3_download")
    if not bpred.empty:
        st.download_button("Download shortfall forecast CSV", bpred.to_csv(index=False), file_name="supply_demand_shortfall_forecast.csv", mime="text/csv", key="g3_balance_download")
else:
    st.info("Select states, choose a horizon and click Update supply / demand outlook.")
st.markdown('<hr class="section-rule"/>', unsafe_allow_html=True)
st.caption("Visual QA dashboard. The production frontend should call the same FastAPI endpoints. Weather / El Niño / cyclone scenarios are intentionally not included yet.")

# ---------------------------------------------------------------------------
# 🧅 Onion Intelligence — floating read-only chat widget (Architecture A)
#    Bottom-right FAB toggles an overlay chat panel in the same space.
#    Only new code is appended here; every section above is unchanged.
# ---------------------------------------------------------------------------
import json as _chat_json  # local alias to avoid touching top-of-file imports


CHAT_OPEN_KEY = "chat_open"
CHAT_SID_KEY = "chat_session_id"
CHAT_MSGS_KEY = "chat_msgs"
CHAT_PICK_KEY = "chat_history_pick"
CHAT_PANEL_TITLE = "🧅 Onion Intelligence"


def _chat_token() -> str:
    return os.getenv("API_BEARER_TOKEN", "").strip()


def _chat_post(message: str) -> dict:
    token = _chat_token()
    if not token:
        raise RuntimeError("API_BEARER_TOKEN is missing from the dashboard environment.")
    response = requests.post(
        f"{api_base}/api/chat",
        json={"message": message, "session_id": st.session_state.get(CHAT_SID_KEY)},
        headers={"Authorization": f"Bearer {token}"},
        timeout=1800,
    )
    try:
        body = response.json()
    except ValueError:
        body = {"message": response.text[:400]}
    if not response.ok:
        detail = body.get("message") or body.get("detail")
        if isinstance(detail, dict):
            detail = detail.get("message") or _chat_json.dumps(detail, ensure_ascii=False)
        raise RuntimeError(str(detail) or f"HTTP {response.status_code}")
    return body


def _chat_sessions_list(limit: int = 12) -> list[dict]:
    token = _chat_token()
    if not token:
        return []
    try:
        response = requests.get(
            f"{api_base}/api/chat/sessions", params={"limit": limit},
            headers={"Authorization": f"Bearer {token}"}, timeout=30,
        )
        if response.ok:
            return response.json().get("sessions", [])
    except Exception:
        pass
    return []


def _chat_load_history(session_id: str) -> list[dict]:
    token = _chat_token()
    if not token:
        return []
    try:
        response = requests.get(
            f"{api_base}/api/chat/history", params={"session_id": session_id},
            headers={"Authorization": f"Bearer {token}"}, timeout=30,
        )
        if response.ok:
            rows = response.json().get("messages", [])
            return [{"role": r["role"], "content": r["content"], "chart": None,
                     "tools_used": _chat_json.loads(r.get("tools_used") or "[]"),
                     "warnings": []} for r in rows]
    except Exception:
        pass
    return []


def _render_chat_chart(chart_payload: dict) -> None:
    """Draw the graph payload returned by the chat API (same contract as Graphs 1/2/3)."""
    graph = chart_payload.get("graph")
    if graph in ("ap_price_forecast", "state_price_comparison"):
        forecast_df = pd.DataFrame(chart_payload.get("forecast", []))
        history_df = pd.DataFrame(chart_payload.get("historical", []))
        fig = go.Figure()
        if not forecast_df.empty:
            color_by_type = dict(PRICE_COLORS)
            for (state, price_type) in sorted(
                    forecast_df[["state", "price_type"]].drop_duplicates().itertuples(index=False, name=None)):
                color = color_by_type.get(price_type, "#4469a8")
                group = f"{state}-{price_type}"
                forecast = forecast_df[(forecast_df["state"] == state) & (forecast_df["price_type"] == price_type)].sort_values("date")
                if not history_df.empty and {"state", "price_type"} <= set(history_df.columns):
                    actual = history_df[(history_df["state"] == state) & (history_df["price_type"] == price_type)].sort_values("date")
                    if not actual.empty:
                        fig.add_trace(go.Scatter(
                            x=actual["date"], y=actual["actual_price_rs_kg"], mode="lines",
                            name=f"{state} · {price_type} · actual",
                            line={"color": color, "width": 2},
                            hovertemplate="%{x}<br>Actual ₹%{y:.2f}/kg<extra></extra>"))
                fig.add_trace(go.Scatter(x=forecast["date"], y=forecast["p90_rs_kg"], mode="lines",
                                         line={"color": color, "width": 1, "dash": "dot"},
                                         showlegend=False, legendgroup=group,
                                         hovertemplate="%{x}<br>Upper bound: ₹%{y:.2f}/kg<extra></extra>"))
                fig.add_trace(go.Scatter(x=forecast["date"], y=forecast["p10_rs_kg"], mode="lines",
                                         line={"color": color, "width": 1, "dash": "dot"},
                                         fill="tonexty",
                                         fillcolor="rgba(68,105,168,0.13)", showlegend=False,
                                         legendgroup=group,
                                         hovertemplate="%{x}<br>Lower bound: ₹%{y:.2f}/kg<extra></extra>"))
                confidence = forecast["confidence_score"] if "confidence_score" in forecast else None
                confidence_line = "<br>Confidence score: %{customdata:.1f}%" if confidence is not None else ""
                fig.add_trace(go.Scatter(x=forecast["date"], y=forecast["p50_rs_kg"],
                                         mode="lines", name=f"{state} · {price_type} · predicted price",
                                         line={"color": color, "width": 2.4, "dash": "dash"},
                                         customdata=confidence,
                                         legendgroup=group,
                                         hovertemplate="%{x}<br>Predicted price: ₹%{y:.2f}/kg"
                                         + confidence_line + "<extra></extra>"))
            st.caption("The shaded band shows the lower-to-upper forecast range.")
        threshold = chart_payload.get("threshold_rs_kg")
        if threshold:
            fig.add_hline(y=threshold, line_dash="dot", line_color="#a05a5a",
                          annotation_text=f"₹{threshold}/kg",
                          annotation_font_color="#a05a5a")
        configure_chart(fig, y_title="Price (₹/kg)", height=380)
        st.plotly_chart(fig, use_container_width=True)
    elif graph == "supply_demand_shortfall":
        quantity_df = pd.DataFrame(chart_payload.get("quantity_forecast", []))
        balance_df = pd.DataFrame(chart_payload.get("balance_forecast", []))
        fig = go.Figure()
        colors = {"production": "#18846B", "consumption": "#4469A8", "arrival": "#C78422"}
        if not quantity_df.empty:
            for metric in ("production", "consumption", "arrival"):
                series = quantity_df[quantity_df["metric"] == metric].sort_values("date")
                if series.empty:
                    continue
                fig.add_trace(go.Scatter(
                    x=series["date"], y=series["predicted_quantity_tonnes_month"],
                    mode="lines+markers", name=f"Predicted {metric}",
                    line={"color": colors[metric], "width": 2.2, "dash": "dash"},
                    hovertemplate="%{x|%b %Y}<br>%{y:,.0f} t/month<extra></extra>"))
        if not balance_df.empty:
            fig.add_trace(go.Bar(
                x=balance_df["date"], y=balance_df["shortfall_p50_tonnes_month"],
                name="Predicted shortfall", marker_color="#A63D40",
                hovertemplate="%{x|%b %Y}<br>Predicted shortfall: %{y:,.1f} t/month<extra></extra>"))
        configure_chart(fig, y_title="Tonnes per month", height=380)
        st.plotly_chart(fig, use_container_width=True)
    else:
        rows = (chart_payload.get("forecast")
                or chart_payload.get("forecast_values")
                or chart_payload.get("rows"))
        if rows:
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        else:
            st.caption("This answer has no chartable rows.")


def _render_chat_message(item: dict) -> None:
    with st.chat_message("user" if item.get("role") == "user" else "assistant"):
        st.markdown(item.get("content", ""))
        chart = item.get("chart")
        if chart:
            with st.expander("📊 Show forecast chart", expanded=False):
                _render_chat_chart(chart)
        tools_used = item.get("tools_used") or []
        if tools_used:
            st.caption("🔧 " + ", ".join(tools_used))
        for warning in item.get("warnings") or []:
            st.caption("⚠️ " + str(warning))


def _chat_css() -> None:
    is_open = bool(st.session_state.get(CHAT_OPEN_KEY))
    hide = "" if is_open else "  div.st-key-chat_panel { display: none; }"
    st.markdown(f"""
<style>
  /* Floating chat FAB */
  div.st-key-chat_fab {{ position: fixed !important; bottom: 24px !important; right: 24px !important;
      z-index: 9999; width: auto !important; height: auto !important; }}
  div.st-key-chat_fab button {{ display: flex; align-items: center; justify-content: center;
      width: auto !important; min-width: 168px; height: 54px; min-height: 54px;
      padding: 0 20px; border-radius: 999px; font-size: 16px; font-weight: 700;
      line-height: 1.2; white-space: nowrap;
      box-shadow: 0 8px 24px rgba(15,35,65,.32); background: #174a7e; color: #fff;
      border: 2px solid #fff; }}
  div.st-key-chat_fab button:hover {{ background: #10385f; color: #fff;
      border-color: #fff; transform: translateY(-2px); }}
  div.st-key-chat_fab button:focus-visible {{ outline: 3px solid #f4a340; outline-offset: 3px; }}
  /* Floating chat panel */
  div.st-key-chat_panel {{ position: fixed; bottom: 80px; right: 18px; z-index: 9998;
      display: flex; flex-direction: column; width: min(470px, calc(100vw - 36px));
      height: min(660px, 78vh); min-height: 240px; overflow: hidden;
      background: #ffffff; border: 1px solid #dfe4ec; border-radius: 16px;
      padding: 14px 16px 10px; box-shadow: 0 18px 50px rgba(22,28,40,.25); }}
  div.st-key-chat_panel > [data-testid="stLayoutWrapper"]:has(.st-key-chat_messages) {{
      flex: 1 1 auto; min-height: 0; overflow: hidden; }}
  div.st-key-chat_panel div.st-key-chat_messages {{ height: 100%; min-height: 0;
      overflow-y: auto; overflow-x: hidden; }}
  div.st-key-chat_panel [data-testid="stElementContainer"].st-key-chat_input {{
      flex: 0 0 auto; padding-top: 8px; background: #fff; }}
  @media (max-width: 540px) {{
    div.st-key-chat_fab {{ bottom: 16px !important; right: 16px !important; }}
    div.st-key-chat_panel {{ width: calc(100vw - 24px); right: 12px; bottom: 78px;
        height: min(660px, calc(100vh - 100px)); }}
  }}
{hide}
</style>
""", unsafe_allow_html=True)


st.session_state.setdefault(CHAT_OPEN_KEY, False)
st.session_state.setdefault(CHAT_SID_KEY, None)
st.session_state.setdefault(CHAT_MSGS_KEY, [])

_chat_css()

# FAB — bottom-right, toggles the panel.
fab_label = "✕ Close chat" if st.session_state.get(CHAT_OPEN_KEY) else "💬 Ask Onion AI"
if st.button(fab_label, key="chat_fab", use_container_width=False):
    st.session_state[CHAT_OPEN_KEY] = not st.session_state.get(CHAT_OPEN_KEY, False)
    st.rerun()

panel = st.container(key="chat_panel", border=False)
with panel:
    if st.session_state.get(CHAT_OPEN_KEY):
        st.markdown(f'<div class="chat-panel-title">{CHAT_PANEL_TITLE}</div>', unsafe_allow_html=True)
        st.caption("Read-only assistant · forecasts & history via your Graph APIs")

        new_chat, history_pick, _spacer = st.columns([1.1, 2.2, 0.8])
        with new_chat:
            if st.button("New chat", key="chat_new", use_container_width=False):
                st.session_state[CHAT_SID_KEY] = None
                st.session_state[CHAT_MSGS_KEY] = []
                st.session_state[CHAT_PICK_KEY] = "＋ New chat"
                st.rerun()
        with history_pick:
            saved = _chat_sessions_list()
            option_by_id = {}
            options = ["＋ New chat"]
            for session_row in saved:
                label = f"{session_row.get('title', 'chat')} · {str(session_row.get('updated_at', ''))[:16]}"
                options.append(label)
                option_by_id[label] = session_row["session_id"]
            pick = st.selectbox(
                "History", options, key="chat_pick",
                index=options.index(st.session_state.get(CHAT_PICK_KEY, "＋ New chat"))
                if st.session_state.get(CHAT_PICK_KEY, "＋ New chat") in options else 0,
                label_visibility="collapsed",
            )
            if pick != st.session_state.get(CHAT_PICK_KEY, "＋ New chat"):
                st.session_state[CHAT_PICK_KEY] = pick
                if pick == "＋ New chat":
                    st.session_state[CHAT_SID_KEY] = None
                    st.session_state[CHAT_MSGS_KEY] = []
                else:
                    session_id = option_by_id.get(pick)
                    st.session_state[CHAT_SID_KEY] = session_id
                    st.session_state[CHAT_MSGS_KEY] = _chat_load_history(session_id)
                st.rerun()

        chat_items = st.session_state.get(CHAT_MSGS_KEY, [])
        with st.container(key="chat_messages"):
            if chat_items:
                for item in chat_items:
                    _render_chat_message(item)
            else:
                st.caption("Ask a question to start chatting about onion prices and supply.")

        prompt = st.chat_input("Ask about onion prices, supply or freshness…",
                               key="chat_input", max_chars=2000)
        if prompt:
            user_item = {"role": "user", "content": prompt, "chart": None,
                         "tools_used": [], "warnings": []}
            st.session_state[CHAT_MSGS_KEY].append(user_item)
            _render_chat_message(user_item)
            with st.chat_message("assistant"):
                with st.spinner("🧅 Thinking…"):
                    try:
                        body = _chat_post(prompt)
                        st.session_state[CHAT_SID_KEY] = body.get("session_id", st.session_state.get(CHAT_SID_KEY))
                        assistant_item = {
                            "role": "assistant",
                            "content": body.get("answer") or "_Empty answer._",
                            "chart": body.get("chart"),
                            "tools_used": body.get("tools_used") or [],
                            "warnings": body.get("warnings") or [],
                        }
                    except Exception as chat_error:
                        assistant_item = {
                            "role": "assistant",
                            "content": f"⚠️ Chat failed: {chat_error}",
                            "chart": None, "tools_used": [], "warnings": [],
                        }
            st.session_state[CHAT_MSGS_KEY].append(assistant_item)
            _render_chat_message(assistant_item)
    else:
        st.caption("Open the chat with the 🧅 button at the bottom-right.")
