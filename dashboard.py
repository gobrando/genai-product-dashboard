"""
Phoenix PM Dashboard — Streamlit Dashboard
Zero-config analytics for any GenAI product traced with Phoenix Arize.
Just paste your Phoenix URL and go.
"""
import streamlit as st
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import os
import re
from dotenv import load_dotenv

from phoenix_client import PhoenixClient
from data_analyzer import TraceAnalyzer
from config import (
    load_config,
    get_all_meeting_starts,
    get_cohort_emails,
    get_location_for_email,
    get_all_cohort_emails,
    DashboardConfig,
    CohortConfig,
    LocationConfig,
)

# Load environment variables
load_dotenv()

# Load product configuration (gracefully returns defaults when config.yaml absent)
CONFIG = load_config()

# Page configuration — use session-state product name when available
_initial_product_name = CONFIG.product_name or "My AI Product"

st.set_page_config(
    page_title=f"{_initial_product_name} — GenAI Product Dashboard",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Custom CSS for better styling
st.markdown("""
    <style>
    .big-metric {
        font-size: 2.5rem !important;
        font-weight: bold !important;
    }
    .metric-label {
        font-size: 0.9rem !important;
        color: #666 !important;
    }
    </style>
""", unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DEFAULT_COLORS = [
    "#1f77b4", "#2ca02c", "#ff7f0e", "#9467bd", "#17becf",
    "#d62728", "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22",
]

# Common timezones for the meeting-window picker
_COMMON_TIMEZONES = [
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "America/Anchorage",
    "Pacific/Honolulu",
    "UTC",
    "Europe/London",
    "Europe/Berlin",
    "Europe/Paris",
    "Asia/Tokyo",
    "Asia/Shanghai",
    "Asia/Kolkata",
    "Australia/Sydney",
]


def _cohort_color(idx: int, cohort) -> str:
    """Return the configured color for a cohort, falling back to a palette."""
    if hasattr(cohort, "color") and cohort.color:
        return cohort.color
    return DEFAULT_COLORS[idx % len(DEFAULT_COLORS)]


@st.cache_resource
def get_phoenix_client(api_url, api_key):
    """Initialize Phoenix client (cached)."""
    return PhoenixClient(api_url, api_key)


@st.cache_data(ttl=300)
def _fetch_projects(api_url, api_key):
    """Fetch available projects from Phoenix (cached 5 min)."""
    try:
        client = PhoenixClient(api_url, api_key)
        projects = client.get_projects_graphql()
        if not projects:
            projects = client.get_projects()
        return projects
    except Exception:
        return []


def _extract_project_id_from_url(url: str) -> str:
    """Extract Phoenix project ID from URLs like .../projects/<id>/spans."""
    if not url:
        return ""
    m = re.search(r"/projects/([^/]+)/spans", url)
    return m.group(1) if m else ""


def _normalize_api_base_url(url: str) -> str:
    """
    Normalize Phoenix URL to base host.
    Supports users pasting either:
      - base URL:  https://phoenix.example.com:6006
      - full spans URL: https://phoenix.example.com:6006/projects/<id>/spans
    """
    if not url:
        return ""
    return re.sub(r"/projects/[^/]+/spans/?$", "", url.strip()).rstrip("/")


def _parse_emails(text: str) -> set:
    return {e.strip().lower() for e in (text or "").splitlines() if e.strip()}


def _detect_trace_types(df: pd.DataFrame) -> list:
    """Return sorted unique trace types found in a DataFrame."""
    if df is None or df.empty or "trace_type" not in df.columns:
        return []
    return sorted(
        [t for t in df["trace_type"].dropna().unique().tolist() if t and t != "other"]
    )


def _build_cohort_email_sets_from_session() -> dict:
    """Return {cohort_name: set_of_emails} built from session-state UI inputs."""
    raw = st.session_state.get("_cohort_definitions", "")
    cohorts = _parse_cohort_definitions(raw)
    return {name: emails for name, emails in cohorts.items()}


def _build_cohort_email_sets(config: DashboardConfig) -> dict:
    """Return {cohort_name: set_of_emails} for all configured cohorts."""
    return {
        cohort.name: {e.lower().strip() for e in cohort.emails}
        for cohort in config.cohorts
    }


def _parse_cohort_definitions(text: str) -> dict:
    """Parse a cohort definition block into {name: set_of_emails}.

    Expected format (one or more sections):
        Cohort Name
        email1@example.com
        email2@example.com

        Another Cohort
        email3@example.com
    """
    cohorts: dict = {}
    current_name = None
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            current_name = None
            continue
        if "@" in stripped:
            if current_name is None:
                # Treat lines before any header as unnamed — skip
                continue
            cohorts.setdefault(current_name, set()).add(stripped.lower())
        else:
            # It is a cohort name header
            current_name = stripped
            cohorts.setdefault(current_name, set())
    return cohorts


def _get_product_name() -> str:
    """Return the product name from session state, falling back to CONFIG."""
    return st.session_state.get("_product_name", CONFIG.product_name or "My AI Product")


def _domain_from_email(email: str) -> str:
    """Extract domain from an email address."""
    email = str(email).strip().lower()
    if "@" in email:
        return email.split("@", 1)[1]
    return "unknown"


def _build_location_mapping_from_session() -> dict:
    """Return {domain: friendly_name} from the session-state location text."""
    raw = st.session_state.get("_location_domain_map", "")
    mapping: dict = {}
    for line in (raw or "").splitlines():
        stripped = line.strip()
        if not stripped or "=" not in stripped:
            continue
        parts = stripped.split("=", 1)
        domain = parts[0].strip().lower()
        name = parts[1].strip()
        if domain and name:
            mapping[domain] = name
    return mapping


def _get_location_for_email_dynamic(
    email: str,
    config: DashboardConfig,
    cohort_email_sets: dict,
    domain_map: dict,
) -> str:
    """Determine location, preferring session-state domain mapping, then config."""
    email_lower = email.lower().strip() if email else ""
    if not email_lower or "unknown" in email_lower or "@" not in email_lower:
        return "Unknown"

    # 1. Check session-state domain map
    domain = _domain_from_email(email_lower)
    if domain in domain_map:
        return domain_map[domain]

    # 2. Fall through to config-based location
    return get_location_for_email(config, email_lower, cohort_email_sets)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

@st.cache_data(ttl=600)  # Cache for 10 minutes
def load_data(
    _client,
    project_id,
    start_time,
    end_time,
    max_spans,
    cache_bust: str = "0",
    _secondary_client=None,
    secondary_project_id: str = "",
):
    """Load spans data from one or two Phoenix projects and dedupe by span identity."""
    if start_time and end_time and end_time <= start_time:
        raise ValueError("end_time must be after start_time")

    primary_spans = _client.get_all_spans(
        project_id=project_id,
        start_time=start_time,
        end_time=end_time,
        max_spans=max_spans,
    )

    secondary_spans = []
    if _secondary_client and secondary_project_id:
        secondary_spans = _secondary_client.get_all_spans(
            project_id=secondary_project_id,
            start_time=start_time,
            end_time=end_time,
            max_spans=max_spans,
        )

    merged = []
    seen = set()
    for span in (primary_spans or []) + (secondary_spans or []):
        dedupe_key = (
            str(span.get("span_id", "")),
            str(span.get("trace_id", "")),
            str(span.get("name", "")),
            str(span.get("start_time", "")),
            str(span.get("end_time", "")),
            str(span.get("parent_id", "")),
        )
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        merged.append(span)

    def _span_start_key(s):
        t = pd.to_datetime(s.get("start_time"), errors="coerce", utc=True)
        return t if pd.notna(t) else pd.Timestamp.min.tz_localize("UTC")

    merged.sort(key=_span_start_key, reverse=True)
    if len(merged) > max_spans:
        merged = merged[:max_spans]

    source_stats = {
        "primary_count": len(primary_spans or []),
        "secondary_count": len(secondary_spans or []),
        "merged_count": len(merged),
        "primary_latest": max(
            [s.get("start_time") for s in (primary_spans or []) if s.get("start_time")],
            default=None,
        ),
        "secondary_latest": max(
            [s.get("start_time") for s in (secondary_spans or []) if s.get("start_time")],
            default=None,
        ),
    }
    return {"spans": merged, "source_stats": source_stats}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    product_name = _get_product_name()
    st.title(f"📊 {product_name} — GenAI Product Dashboard")
    st.markdown("*Analyze your GenAI product usage and quality metrics from Phoenix Arize trace logs*")

    # ------------------------------------------------------------------
    # Sidebar configuration
    # ------------------------------------------------------------------

    # Initialize saved credentials from env or previous session
    if "_saved_url" not in st.session_state:
        st.session_state["_saved_url"] = os.getenv("PHOENIX_API_URL", "")
    if "_saved_api_key" not in st.session_state:
        st.session_state["_saved_api_key"] = os.getenv("PHOENIX_API_KEY", "")
    if "_saved_project_id" not in st.session_state:
        st.session_state["_saved_project_id"] = os.getenv("PHOENIX_PROJECT_ID", "")

    with st.sidebar:
        st.header("⚙️ Configuration")

        # API Configuration — persisted via session state keys
        raw_url = st.text_input(
            "Phoenix URL",
            key="_saved_url",
            placeholder="https://phoenix.example.com:6006 or full spans URL",
            help="Paste your Phoenix instance URL or a full spans URL (e.g. .../projects/ABC/spans).",
        )

        # Auto-extract project ID from pasted URL
        auto_project_id = _extract_project_id_from_url(raw_url)
        api_url = _normalize_api_base_url(raw_url)

        api_key = st.text_input(
            "API Key (optional)",
            key="_saved_api_key",
            type="password",
            help="Leave empty if no authentication required",
        )

        # Project selection — auto-discover from Phoenix when possible
        env_project_id = os.getenv("PHOENIX_PROJECT_ID", "")
        saved_project_id = st.session_state.get("_saved_project_id", "")
        discovered_projects = []
        if api_url and api_url.startswith("http"):
            discovered_projects = _fetch_projects(api_url, api_key if api_key else None)

        if discovered_projects:
            # Sort: non-default projects first, then alphabetically by name
            discovered_projects.sort(
                key=lambda p: (p["name"].lower() == "default", p["name"].lower())
            )

            # Build options: "name (id)" for display, actual id for value
            project_options = [
                {"label": f"{p['name']} ({p['id']})", "id": p["id"], "name": p["name"]}
                for p in discovered_projects
            ]
            display_labels = [p["label"] for p in project_options]

            # Determine default selection: prefer saved > env > URL-extracted > first non-default
            default_idx = 0
            preferred_id = saved_project_id or env_project_id or auto_project_id
            if preferred_id:
                for i, p in enumerate(project_options):
                    if p["id"] == preferred_id:
                        default_idx = i
                        break

            selected_label = st.selectbox(
                "Project",
                options=display_labels,
                index=default_idx,
                key="_project_selector",
                help="Projects discovered from your Phoenix instance. Select the project that contains your product's traces.",
            )
            project_id = project_options[display_labels.index(selected_label)]["id"]
            # Persist the selection
            st.session_state["_saved_project_id"] = project_id
        else:
            # Fallback to manual text input
            project_id = st.text_input(
                "Project ID",
                value=saved_project_id or env_project_id or auto_project_id,
                help="Auto-filled when you paste a full spans URL. Or enter your Phoenix project ID manually.",
            )

        st.divider()

        # Data filters
        st.header("🔍 Data Filters")

        time_range = st.selectbox(
            "Time Range",
            ["Last 24 Hours", "Last 7 Days", "Last 30 Days", "Custom"],
        )

        if time_range == "Custom":
            start_date = st.date_input(
                "Start Date",
                value=datetime.now(timezone.utc).date() - timedelta(days=7),
            )
            end_date = st.date_input(
                "End Date",
                value=datetime.now(timezone.utc).date(),
            )
            start_time = datetime.combine(start_date, datetime.min.time()).replace(tzinfo=timezone.utc)
            end_time = datetime.combine(end_date, datetime.max.time()).replace(tzinfo=timezone.utc)
        else:
            days_map = {"Last 24 Hours": 1, "Last 7 Days": 7, "Last 30 Days": 30}
            days = days_map.get(time_range, 7)
            end_time = datetime.now(timezone.utc)
            start_time = end_time - timedelta(days=days)

        # Max spans is computed automatically based on date range
        days_in_range = max((end_time - start_time).days, 1)
        max_spans = min(max(days_in_range * 500, 10000), 100000)

        with st.expander("⚙️ Advanced", expanded=False):
            st.caption(f"Auto-calculated: {max_spans:,} spans for {days_in_range} days")
            user_override = st.number_input(
                "Max Spans to Load",
                min_value=1000,
                max_value=100000,
                value=max_spans,
                step=5000,
                key="_max_spans_override",
                help="Auto-calculated from your date range. Increase if data appears incomplete.",
            )
            # Use the larger of auto-calculated and user override
            max_spans = max(max_spans, user_override)

        # Product name
        st.text_input(
            "Product Name",
            value=CONFIG.product_name or "My AI Product",
            key="_product_name",
            help="Displayed in charts and headers.",
        )

        # Secondary source — collapsed by default
        with st.expander("🔗 Secondary Source (optional)", expanded=False):
            has_secondary = bool(CONFIG.secondary_phoenix_url)
            secondary_url_default = os.getenv(
                "PHOENIX_DEV_SPANS_URL",
                CONFIG.secondary_phoenix_url,
            )
            include_secondary_source = st.checkbox(
                "Include secondary source traces",
                value=has_secondary,
                help="If enabled, dashboard also loads spans from a secondary Phoenix project and merges with primary data.",
            )
            secondary_url = st.text_input(
                "Secondary Phoenix URL",
                value=secondary_url_default,
                help="Can be full spans URL (recommended) or base API URL.",
                disabled=not include_secondary_source,
            )
            secondary_project_default = os.getenv(
                "PHOENIX_PROJECT_ID_DEV",
                _extract_project_id_from_url(secondary_url_default) or CONFIG.secondary_project_id,
            )
            secondary_project_id = st.text_input(
                "Secondary Project ID",
                value=secondary_project_default,
                help="Project ID for the secondary Phoenix source.",
                disabled=not include_secondary_source,
            )

        st.divider()

        # Load / refresh controls
        # Cache bust key: changes whenever Load Data is clicked so stale data is never served
        if "cache_bust" not in st.session_state:
            st.session_state.cache_bust = "0"

        load_button = st.button("🔄 Load Data", type="primary", use_container_width=True)
        if load_button:
            # Always bust the cache on explicit load
            st.session_state.cache_bust = str(datetime.now(timezone.utc).timestamp())

    # ------------------------------------------------------------------
    # Session state
    # ------------------------------------------------------------------
    if "analyzer" not in st.session_state:
        st.session_state.analyzer = None
    if "data_loaded" not in st.session_state:
        st.session_state.data_loaded = False

    # ------------------------------------------------------------------
    # Load data
    # ------------------------------------------------------------------
    if load_button or st.session_state.data_loaded:
        # Guard: empty URL
        if not api_url or not api_url.startswith("http"):
            if load_button:
                st.warning(
                    "Please paste a valid Phoenix URL in the sidebar to get started. "
                    "It should look like `https://phoenix.example.com:6006` or a full "
                    "spans URL such as `https://phoenix.example.com:6006/projects/ABC/spans`."
                )
            if not st.session_state.data_loaded:
                # Show landing page
                _show_landing_page()
                return

        try:
            with st.spinner("Connecting to Phoenix API..."):
                primary_base_url = _normalize_api_base_url(api_url)
                client = get_phoenix_client(primary_base_url, api_key if api_key else None)
                secondary_client = None
                secondary_project_final = ""
                if include_secondary_source and secondary_url:
                    secondary_base_url = _normalize_api_base_url(secondary_url)
                    if secondary_base_url:
                        secondary_client = get_phoenix_client(
                            secondary_base_url, api_key if api_key else None
                        )
                        secondary_project_final = (
                            secondary_project_id.strip()
                            or _extract_project_id_from_url(secondary_url)
                        )

            with st.spinner("Loading trace data..."):
                load_result = load_data(
                    client,
                    project_id if project_id else None,
                    start_time,
                    end_time,
                    max_spans,
                    st.session_state.get("cache_bust", "0"),
                    secondary_client,
                    secondary_project_final,
                )
                spans = load_result.get("spans", []) if isinstance(load_result, dict) else load_result
                source_stats = load_result.get("source_stats", {}) if isinstance(load_result, dict) else {}

            if not spans:
                msg = "No data found for the selected time range."
                if not project_id:
                    msg += " **No project selected** — try selecting a project from the dropdown, or paste a full spans URL."
                else:
                    msg += f" Project ID: `{project_id}`. Try a wider time range or check that this project has trace data."
                st.error(msg)
                return

            st.session_state.analyzer = TraceAnalyzer(spans, config=CONFIG)
            st.session_state.source_stats = source_stats
            st.session_state.data_loaded = True
            st.success(f"Loaded {len(spans)} spans successfully!")
            if source_stats:
                st.caption(
                    "Source counts — "
                    f"Primary: {source_stats.get('primary_count', 0):,} "
                    f"(latest: {source_stats.get('primary_latest', 'n/a')}), "
                    f"Secondary: {source_stats.get('secondary_count', 0):,} "
                    f"(latest: {source_stats.get('secondary_latest', 'n/a')}), "
                    f"Merged used by dashboard: {source_stats.get('merged_count', len(spans)):,}."
                )

        except Exception as e:
            st.error(f"Error loading data: {str(e)}")
            return

    if not st.session_state.data_loaded:
        _show_landing_page()
        return

    analyzer = st.session_state.analyzer
    source_stats = st.session_state.get("source_stats", {})
    if source_stats:
        st.caption(
            "Loaded sources — "
            f"Primary: {source_stats.get('primary_count', 0):,} spans "
            f"(latest: {source_stats.get('primary_latest', 'n/a')}), "
            f"Secondary: {source_stats.get('secondary_count', 0):,} spans "
            f"(latest: {source_stats.get('secondary_latest', 'n/a')})."
        )

    # --- Date coverage check ---
    if (
        not analyzer.traces_df.empty
        and "trace_start" in analyzer.traces_df.columns
    ):
        _ts_col = pd.to_datetime(analyzer.traces_df["trace_start"], errors="coerce", utc=True)
        _actual_end = _ts_col.max()
        _actual_start = _ts_col.min()
        if pd.notna(_actual_end) and pd.notna(end_time):
            _end_time_ts = pd.Timestamp(end_time, tz="UTC") if not hasattr(end_time, "tzinfo") or end_time.tzinfo is None else pd.Timestamp(end_time)
            if _actual_end < _end_time_ts - pd.Timedelta(hours=12):
                _total_spans = len(analyzer.df) if analyzer.df is not None else 0
                _spans_hit_limit = _total_spans >= max_spans
                if _spans_hit_limit:
                    st.warning(
                        f"Data only covers through **{_actual_end.strftime('%Y-%m-%d %H:%M UTC')}**. "
                        f"The span limit ({max_spans:,}) was reached — expand **Advanced** in the sidebar to increase it."
                    )
                else:
                    st.info(
                        f"Latest trace in this project is from **{_actual_end.strftime('%Y-%m-%d %H:%M UTC')}**. "
                        f"No newer data exists in Phoenix for the selected project. "
                        f"If you expect more recent data, check that your product is sending traces to this project, "
                        f"or try selecting a different project."
                    )

    # --- Global outlier thresholds for latency charts (Fix 2) ---
    if not analyzer.traces_df.empty and "trace_duration_s" in analyzer.traces_df.columns:
        _dur = pd.to_numeric(analyzer.traces_df["trace_duration_s"], errors="coerce").dropna()
        _dur = _dur[_dur > 0]
        if not _dur.empty:
            _latency_p95 = float(_dur.quantile(0.95))
            _latency_p99 = float(_dur.quantile(0.99))
            _latency_median = float(_dur.median())
            _outlier_cap = max(_latency_p99, _latency_p95 * 3)
        else:
            _latency_p95 = _latency_p99 = _latency_median = _outlier_cap = 60.0
    else:
        _latency_p95 = _latency_p99 = _latency_median = _outlier_cap = 60.0

    # Detect trace types dynamically from data
    detected_trace_types = _detect_trace_types(analyzer.traces_df)

    # Build cohort email sets — merge config + session-state UI
    cohort_email_sets = _build_cohort_email_sets(CONFIG)
    ui_cohort_sets = _build_cohort_email_sets_from_session()
    for name, emails in ui_cohort_sets.items():
        if name in cohort_email_sets:
            cohort_email_sets[name] = cohort_email_sets[name] | emails
        else:
            cohort_email_sets[name] = emails

    # Build location domain mapping from session state
    domain_map = _build_location_mapping_from_session()

    # ------------------------------------------------------------------
    # Main dashboard tabs
    # ------------------------------------------------------------------
    tab1, tab2, tab3, tab4, tab5, tab6, tab7 = st.tabs([
        "📈 Executive Summary",
        "📊 Usage Analytics",
        "📋 Usage Report",
        "⚡ Performance Metrics",
        "🔎 Log Explorer",
        "🧠 Advanced Analytics",
        "✅ Evals",
    ])

    # ==================================================================
    # TAB 1: Executive Summary
    # ==================================================================
    with tab1:
        st.header("Executive Summary")
        st.markdown("*High-level KPIs for leadership reporting*")

        stats = analyzer.get_macro_statistics()

        if stats:
            # Key metrics row
            col1, col2, col3, col4 = st.columns(4)
            with col1:
                st.metric("Total Requests", f"{stats['total_requests']:,}", help="Total number of API requests")
            with col2:
                st.metric("Unique Traces", f"{stats['unique_traces']:,}", help="Number of unique user sessions")
            with col3:
                st.metric("Success Rate", f"{stats['success_rate']:.1f}%", help="Percentage of successful requests")
            with col4:
                st.metric("Avg Latency", f"{stats['avg_latency_s']:.2f}s", help="Average response time")

            st.divider()

            col1, col2, col3, col4 = st.columns(4)
            with col1:
                st.metric("Total Tokens", f"{stats['total_tokens']:,}", help="Total tokens consumed")
            with col2:
                st.metric("Avg Tokens/Request", f"{stats['avg_tokens_per_request']:.0f}", help="Average tokens per request")
            with col3:
                st.metric("P95 Latency", f"{stats['p95_latency_s']:.2f}s", help="95th percentile latency")
            with col4:
                st.metric("P99 Latency", f"{stats['p99_latency_s']:.2f}s", help="99th percentile latency")

            st.divider()

            # Trace counts by type — dynamic
            if "trace_counts" in stats:
                st.subheader("Trace Counts by Type")
                trace_counts = stats["trace_counts"]
                # Build columns dynamically for each detected type + "other"
                type_keys = [k for k in trace_counts.keys() if k != "other"]
                if "other" in trace_counts:
                    type_keys.append("other")
                cols = st.columns(min(len(type_keys), 6) or 1)
                for i, ttype in enumerate(type_keys):
                    with cols[i % len(cols)]:
                        label = ttype.replace("_", " ").title()
                        st.metric(label, f"{trace_counts.get(ttype, 0):,}")

            st.divider()

            # Date range
            if stats["date_range"]["start"] and stats["date_range"]["end"]:
                st.info(
                    f"Data Range: {stats['date_range']['start'].strftime('%Y-%m-%d %H:%M')} "
                    f"to {stats['date_range']['end'].strftime('%Y-%m-%d %H:%M')}"
                )

            # Model usage breakdown
            if stats.get("models_used"):
                st.subheader("Model Usage Distribution")
                # Filter out empty/zero model names
                model_items = {
                    k: v for k, v in stats["models_used"].items()
                    if k and str(k).strip() and str(k).strip() != "0"
                }
                if model_items:
                    # Clean up model names: strip date suffixes like "-2025-11-13"
                    cleaned = {}
                    for name, count in model_items.items():
                        clean_name = re.sub(r'-\d{4}-\d{2}-\d{2}$', '', str(name))
                        cleaned[clean_name] = cleaned.get(clean_name, 0) + count
                    model_df = pd.DataFrame(
                        list(cleaned.items()),
                        columns=["Model", "Requests"],
                    )
                    fig = px.pie(model_df, values="Requests", names="Model", title="Requests by Model")
                    st.plotly_chart(fig, use_container_width=True)

                    # Show raw model names for debugging
                    raw_models = stats.get("models_used_raw", stats.get("models_used", {}))
                    if raw_models:
                        with st.expander("Raw model names from traces", expanded=False):
                            raw_df = pd.DataFrame(
                                list(raw_models.items()),
                                columns=["Raw Model Name", "Span Count"],
                            ).sort_values("Span Count", ascending=False)
                            st.dataframe(raw_df, use_container_width=True, hide_index=True)
                            st.caption("These are the exact model names stored in Phoenix LLM span attributes.")

    # ==================================================================
    # TAB 2: Usage Analytics (Trace-based)
    # ==================================================================
    with tab2:
        st.header("Usage Analytics")
        st.markdown("*User-level analytics based on traces*")

        # ----- User Analytics Table -----
        st.subheader("👥 User Usage Summary")
        user_analytics = analyzer.get_user_analytics()

        if not user_analytics.empty:
            # Build display columns dynamically — always include base columns
            base_cols = ["user_name", "user_email", "total_traces"]
            # Add per-type count columns if they exist
            type_count_cols = [
                c for c in user_analytics.columns
                if c.endswith("_count") and c not in ("total_traces",)
            ]
            tail_cols = ["first_trace", "last_trace", "avg_duration_s", "total_tokens"]
            display_cols = base_cols + type_count_cols + tail_cols
            display_cols = [c for c in display_cols if c in user_analytics.columns]

            user_display = user_analytics[display_cols].copy()
            # Build friendly column names
            friendly_names = {
                "user_name": "Name",
                "user_email": "Email",
                "total_traces": "Total Traces",
                "first_trace": "First Trace",
                "last_trace": "Last Trace",
                "avg_duration_s": "Avg Duration (s)",
                "total_tokens": "Total Tokens",
            }
            for c in type_count_cols:
                label = c.replace("_count", "").replace("_", " ").title()
                # Avoid collision with existing friendly names (e.g. "Email" from user_email)
                if label in friendly_names.values():
                    label = f"{label} Traces"
                friendly_names[c] = label
            user_display = user_display.rename(columns=friendly_names)

            for date_col in ["First Trace", "Last Trace"]:
                if date_col in user_display.columns:
                    user_display[date_col] = pd.to_datetime(
                        user_display[date_col], errors="coerce"
                    ).dt.strftime("%Y-%m-%d %H:%M")
            if "Avg Duration (s)" in user_display.columns:
                user_display["Avg Duration (s)"] = user_display["Avg Duration (s)"].round(2).fillna(0)
            user_display = user_display.fillna("N/A")

            st.dataframe(user_display, use_container_width=True, hide_index=True)

            # User selection for detailed view
            st.subheader("🔍 User Detail View")
            selected_user = st.selectbox(
                "Select a user to view detailed trace history",
                options=user_analytics["user_email"].tolist(),
                format_func=lambda x: f"{user_analytics[user_analytics['user_email']==x]['user_name'].iloc[0]} ({x})",
            )

            if selected_user:
                user_traces = analyzer.get_user_trace_details(selected_user)

                if not user_traces.empty:
                    st.write(f"**Traces for {selected_user}**")

                    trace_display_cols = [
                        "trace_start", "trace_type", "query", "category",
                        "location_preference", "zip_code",
                        "trace_duration_s", "total_tokens", "status",
                    ]
                    available_cols = [c for c in trace_display_cols if c in user_traces.columns]
                    trace_display = user_traces[available_cols].copy()
                    col_names = [
                        "Timestamp", "Type", "Query", "Category", "Location Pref",
                        "Zip Code", "Duration (s)", "Tokens", "Status",
                    ]
                    trace_display.columns = col_names[: len(available_cols)]

                    if "Timestamp" in trace_display.columns:
                        trace_display["Timestamp"] = pd.to_datetime(
                            trace_display["Timestamp"], errors="coerce"
                        ).dt.strftime("%Y-%m-%d %H:%M:%S")
                    if "Duration (s)" in trace_display.columns:
                        trace_display["Duration (s)"] = pd.to_numeric(
                            trace_display["Duration (s)"], errors="coerce"
                        ).round(2)
                    trace_display = trace_display.fillna("N/A")
                    st.dataframe(trace_display, use_container_width=True, hide_index=True)

                    # Summary stats — dynamic by type
                    summary_cols = st.columns(max(2 + len(detected_trace_types), 4))
                    with summary_cols[0]:
                        st.metric("Total Traces", len(user_traces))
                    for i, ttype in enumerate(detected_trace_types):
                        with summary_cols[1 + i]:
                            count = len(user_traces[user_traces["trace_type"] == ttype])
                            st.metric(ttype.replace("_", " ").title(), count)
                    with summary_cols[min(1 + len(detected_trace_types), len(summary_cols) - 1)]:
                        avg_dur = user_traces["trace_duration_s"].mean()
                        st.metric("Avg Duration", f"{avg_dur:.2f}s" if pd.notna(avg_dur) else "N/A")

        # ----- Trace volume over time -----
        st.subheader("📈 Trace Volume Over Time")
        col1, col2 = st.columns([1, 3])
        with col1:
            freq = st.selectbox(
                "Time Granularity",
                ["Hourly", "Daily", "Weekly"],
                index=1,
                key="trace_freq",
            )

        freq_map = {"Hourly": "H", "Daily": "D", "Weekly": "W"}
        trace_time_series = analyzer.get_trace_time_series(freq=freq_map[freq])

        if not trace_time_series.empty:
            fig = make_subplots(
                rows=2, cols=1,
                subplot_titles=("Trace Volume Over Time", "Trace Types Breakdown"),
                vertical_spacing=0.15,
            )

            fig.add_trace(
                go.Scatter(
                    x=trace_time_series["timestamp"],
                    y=trace_time_series["trace_count"],
                    name="Total Traces",
                    fill="tozeroy",
                    line=dict(color="#1f77b4"),
                ),
                row=1, col=1,
            )

            # Add a line per detected type
            for i, ttype in enumerate(detected_trace_types):
                col_name = f"{ttype}_count"
                if col_name in trace_time_series.columns:
                    fig.add_trace(
                        go.Scatter(
                            x=trace_time_series["timestamp"],
                            y=trace_time_series[col_name],
                            name=ttype.replace("_", " ").title(),
                            fill="tozeroy",
                            line=dict(color=DEFAULT_COLORS[i % len(DEFAULT_COLORS)]),
                        ),
                        row=2, col=1,
                    )

            fig.update_xaxes(title_text="Time", row=2, col=1)
            fig.update_yaxes(title_text="Trace Count", row=1, col=1)
            fig.update_yaxes(title_text="Count by Type", row=2, col=1)
            fig.update_layout(height=600, showlegend=True)
            st.plotly_chart(fig, use_container_width=True)

            # Median duration over time (robust to outliers)
            st.subheader("⏱️ Median Trace Duration Over Time")
            _dur_cap = 2 * _latency_median if _latency_median > 0 else 60
            fig_duration = px.line(
                trace_time_series,
                x="timestamp",
                y="avg_duration",
                title="Median Trace Duration Over Time",
                labels={"avg_duration": "Duration (s)", "timestamp": "Time"},
            )
            fig_duration.update_layout(yaxis_range=[0, _dur_cap])
            st.plotly_chart(fig_duration, use_container_width=True)

        # ----- Organic usage -----
        st.divider()
        st.subheader("🌿 Organic Usage — Optional: Filter Out Planned Sessions")
        st.caption(
            "If you run demos or scheduled meetings where users try the tool, "
            "you can enter those meeting times below to separate organic from planned usage. "
            "If you leave meeting times blank, all traces are shown as organic."
        )

        # Meeting windows UI — visible, not hidden in expander
        meeting_tz_name = st.selectbox(
            "Meeting timezone",
            options=_COMMON_TIMEZONES,
            index=_COMMON_TIMEZONES.index(CONFIG.meeting_windows.timezone)
            if CONFIG.meeting_windows.timezone in _COMMON_TIMEZONES
            else 0,
            key="organic_tz",
        )

        meeting_duration_min = st.slider(
            "Assumed meeting session length (minutes)",
            min_value=15,
            max_value=180,
            value=CONFIG.meeting_windows.duration_minutes,
            step=15,
            help="Used to convert each meeting start time into an exclusion window.",
        )

        # Load meeting starts from config
        default_meeting_starts = get_all_meeting_starts(CONFIG)
        default_starts_text = "\n".join(default_meeting_starts) if default_meeting_starts else ""

        meeting_starts_text = st.text_area(
            f"Meeting start times ({meeting_tz_name}) — one per line as YYYY-MM-DD HH:MM",
            value=default_starts_text,
            height=120,
            key="meeting_starts_text",
            placeholder="2026-03-10 09:00\n2026-03-12 14:00",
        )

        always_organic_default = "\n".join(CONFIG.meeting_windows.always_organic_emails) if CONFIG.meeting_windows.always_organic_emails else ""
        always_organic_text = st.text_area(
            "Always count as organic (emails, one per line)",
            value=always_organic_default,
            height=80,
            key="always_organic_emails",
            placeholder="boss@company.com",
        )

        exclude_meetings = bool(meeting_starts_text.strip())

        # Build meeting windows in UTC
        meeting_tz = ZoneInfo(meeting_tz_name)
        meeting_starts_all = []
        if meeting_starts_text.strip():
            for line in meeting_starts_text.splitlines():
                s = line.strip()
                if s:
                    meeting_starts_all.append(s)

        windows_utc = []
        for s in meeting_starts_all:
            try:
                dt_local = datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=meeting_tz)
                start_utc = dt_local.astimezone(timezone.utc)
                end_utc = start_utc + timedelta(minutes=int(meeting_duration_min))
                windows_utc.append((start_utc, end_utc))
            except Exception:
                continue

        if analyzer.traces_df.empty or "trace_start" not in analyzer.traces_df.columns:
            st.info("No trace timestamps available to compute organic usage.")
        else:
            traces_for_org = analyzer.traces_df.copy()
            ts_utc = pd.to_datetime(traces_for_org["trace_start"], errors="coerce", utc=True)
            traces_for_org = traces_for_org.assign(_trace_start_utc=ts_utc).dropna(subset=["_trace_start_utc"])

            planned_mask = pd.Series(False, index=traces_for_org.index)
            if exclude_meetings and windows_utc:
                for start_utc, end_utc in windows_utc:
                    planned_mask = planned_mask | (
                        (traces_for_org["_trace_start_utc"] >= pd.Timestamp(start_utc))
                        & (traces_for_org["_trace_start_utc"] < pd.Timestamp(end_utc))
                    )

                # Always count configured always-organic emails as organic
                always_organic = _parse_emails(always_organic_text)
                always_organic.update(
                    {e.lower().strip() for e in CONFIG.meeting_windows.always_organic_emails}
                )
                if always_organic and "user_email" in traces_for_org.columns:
                    organic_override_mask = (
                        traces_for_org["user_email"]
                        .astype(str)
                        .str.lower()
                        .str.strip()
                        .isin(always_organic)
                    )
                    if organic_override_mask.any():
                        planned_mask = planned_mask & ~organic_override_mask

            organic_df = traces_for_org[~planned_mask].copy()
            planned_df = traces_for_org[planned_mask].copy()

            col1, col2, col3, col4 = st.columns(4)
            with col1:
                st.metric("Total Traces", f"{len(traces_for_org):,}")
            with col2:
                st.metric("Planned (meeting) Traces", f"{len(planned_df):,}")
            with col3:
                st.metric("Organic Traces", f"{len(organic_df):,}")
            with col4:
                pct = (len(organic_df) / len(traces_for_org) * 100) if len(traces_for_org) else 0
                st.metric("% Organic", f"{pct:.1f}%")

            # ----- Uses vs unique users -----
            st.markdown("#### 👥 Uses vs Unique Users (Organic)")
            st.caption(
                "A 'use' equals one trace (any type). "
                "Use the metrics below to compare trace volume vs. active people."
            )

            organic_daily = organic_df.copy()
            organic_daily["day"] = organic_daily["_trace_start_utc"].dt.floor("D")
            daily_uses = organic_daily.groupby("day").size().rename("uses")

            if "user_email" in organic_daily.columns:
                organic_daily["_user_norm"] = (
                    organic_daily["user_email"].astype(str).str.lower().str.strip()
                )
                valid_user_mask = (
                    organic_daily["_user_norm"].ne("")
                    & ~organic_daily["_user_norm"].eq("unknown")
                    & ~organic_daily["_user_norm"].str.startswith("unknown_")
                )
                daily_unique_users = (
                    organic_daily.loc[valid_user_mask]
                    .groupby("day")["_user_norm"]
                    .nunique()
                    .rename("unique_users")
                )
            else:
                daily_unique_users = pd.Series(dtype=float, name="unique_users")

            daily_compare = (
                pd.concat([daily_uses, daily_unique_users], axis=1)
                .fillna(0)
                .reset_index()
                .rename(columns={"day": "timestamp"})
            )
            daily_compare["uses"] = daily_compare["uses"].astype(int)
            daily_compare["unique_users"] = daily_compare["unique_users"].astype(int)
            daily_compare["uses_per_active_user"] = np.where(
                daily_compare["unique_users"] > 0,
                daily_compare["uses"] / daily_compare["unique_users"],
                np.nan,
            )

            avg_uses_per_day = float(daily_compare["uses"].mean()) if not daily_compare.empty else 0.0
            avg_unique_users_per_day = (
                float(daily_compare["unique_users"].mean()) if not daily_compare.empty else 0.0
            )
            avg_uses_per_active_user = (
                float(daily_compare["uses_per_active_user"].dropna().mean())
                if not daily_compare["uses_per_active_user"].dropna().empty
                else 0.0
            )

            # Share of organic traces by type — dynamic
            type_pcts = {}
            if "trace_type" in organic_df.columns and not organic_df.empty:
                organic_type_counts = organic_df["trace_type"].fillna("other").value_counts()
                for ttype in detected_trace_types:
                    type_pcts[ttype] = organic_type_counts.get(ttype, 0) / len(organic_df) * 100

            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Avg Organic Uses / Day", f"{avg_uses_per_day:.1f}")
            m2.metric("Avg Unique Users / Day", f"{avg_unique_users_per_day:.1f}")
            m3.metric("Avg Uses per Active User / Day", f"{avg_uses_per_active_user:.2f}")
            # Build organic mix string dynamically
            mix_parts = []
            for ttype in detected_trace_types[:5]:
                abbrev = ttype[0].upper()
                mix_parts.append(f"{abbrev} {type_pcts.get(ttype, 0):.0f}%")
            mix_str = " | ".join(mix_parts) if mix_parts else "N/A"
            mix_help = ", ".join(
                [f"{t[0].upper()}={t}" for t in detected_trace_types[:5]]
            ) if detected_trace_types else ""
            m4.metric("Organic Mix", mix_str, help=mix_help)

            if not daily_compare.empty:
                fig_people = go.Figure()
                fig_people.add_trace(
                    go.Scatter(
                        x=daily_compare["timestamp"],
                        y=daily_compare["uses"],
                        name="Organic Uses (Traces)",
                        mode="lines+markers",
                        line=dict(color="#2ca02c", width=3),
                    )
                )
                fig_people.add_trace(
                    go.Scatter(
                        x=daily_compare["timestamp"],
                        y=daily_compare["unique_users"],
                        name="Unique Users",
                        mode="lines+markers",
                        line=dict(color="#17becf", width=3),
                    )
                )
                fig_people.update_layout(
                    title="Organic Uses vs Unique Users per Day",
                    height=380,
                    xaxis_title="Time",
                    yaxis_title="Count",
                    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                )
                st.plotly_chart(fig_people, use_container_width=True)

            # ----- Workflow completion (dynamic trace types) -----
            st.markdown("#### 🔁 Workflow Completion (same user, same day)")
            st.caption(
                "Tracks how many organic users complete key workflow steps on the same day."
            )
            if (
                not organic_df.empty
                and "trace_type" in organic_df.columns
                and "user_email" in organic_df.columns
            ):
                workflow_df = organic_df.copy()
                workflow_df["day"] = workflow_df["_trace_start_utc"].dt.floor("D")
                workflow_df["_user_norm"] = (
                    workflow_df["user_email"].astype(str).str.lower().str.strip()
                )
                workflow_df = workflow_df[
                    workflow_df["_user_norm"].ne("")
                    & ~workflow_df["_user_norm"].eq("unknown")
                    & ~workflow_df["_user_norm"].str.startswith("unknown_")
                ].copy()

                if not workflow_df.empty:
                    user_day = (
                        workflow_df.groupby(["day", "_user_norm"])["trace_type"]
                        .agg(lambda s: set(s.dropna().astype(str)))
                        .reset_index(name="trace_types")
                    )

                    # Dynamically create did_<type> columns
                    for ttype in detected_trace_types:
                        col_name = f"did_{ttype}"
                        user_day[col_name] = user_day["trace_types"].apply(lambda t, tt=ttype: tt in t)

                    did_cols = [f"did_{t}" for t in detected_trace_types]
                    if did_cols:
                        user_day["did_full_workflow"] = user_day[did_cols].all(axis=1)
                        user_day["steps_completed"] = user_day[did_cols].astype(int).sum(axis=1)

                        daily_workflow_agg = {"active_users": ("_user_norm", "nunique")}
                        for ttype in detected_trace_types:
                            daily_workflow_agg[f"users_{ttype}"] = (f"did_{ttype}", "sum")
                        daily_workflow_agg["users_full_workflow"] = ("did_full_workflow", "sum")

                        daily_workflow = (
                            user_day.groupby("day")
                            .agg(**daily_workflow_agg)
                            .reset_index()
                            .rename(columns={"day": "timestamp"})
                        )

                        # Conversion rate columns
                        for ttype in detected_trace_types:
                            c = f"users_{ttype}"
                            daily_workflow[f"{c}_rate"] = np.where(
                                daily_workflow["active_users"] > 0,
                                daily_workflow[c] / daily_workflow["active_users"] * 100,
                                0,
                            )

                        # Display avg users/day per type
                        wf_cols = st.columns(min(len(detected_trace_types) + 1, 6))
                        for i, ttype in enumerate(detected_trace_types):
                            with wf_cols[i % len(wf_cols)]:
                                label = ttype.replace("_", " ").title()
                                st.metric(
                                    f"Avg Users/Day - {label}",
                                    f"{daily_workflow[f'users_{ttype}'].mean():.1f}",
                                )
                        with wf_cols[min(len(detected_trace_types), len(wf_cols) - 1)]:
                            st.metric(
                                "Avg Users/Day - Full Workflow",
                                f"{daily_workflow['users_full_workflow'].mean():.1f}",
                            )

                        # Workflow conversion summary
                        st.markdown("**Workflow Conversion Rates (user-days)**")
                        total_user_days = len(user_day)
                        conv_cols = st.columns(min(len(detected_trace_types) + 1, 6))

                        for i, ttype in enumerate(detected_trace_types):
                            did_col = f"did_{ttype}"
                            type_user_days = int(user_day[did_col].sum())
                            rate = type_user_days / total_user_days * 100 if total_user_days else 0.0
                            label = ttype.replace("_", " ").title()
                            with conv_cols[i % len(conv_cols)]:
                                st.metric(f"{label} Reach", f"{rate:.1f}%")

                        full_user_days = int(user_day["did_full_workflow"].sum())
                        full_rate = full_user_days / total_user_days * 100 if total_user_days else 0.0
                        with conv_cols[min(len(detected_trace_types), len(conv_cols) - 1)]:
                            st.metric("Full Workflow Completion", f"{full_rate:.1f}%")

                        # Workflow chart
                        fig_wf = go.Figure()
                        for i, ttype in enumerate(detected_trace_types):
                            fig_wf.add_trace(
                                go.Scatter(
                                    x=daily_workflow["timestamp"],
                                    y=daily_workflow[f"users_{ttype}"],
                                    name=f"Users: {ttype.replace('_', ' ').title()}",
                                    line=dict(
                                        color=DEFAULT_COLORS[i % len(DEFAULT_COLORS)],
                                        width=2,
                                    ),
                                )
                            )
                        fig_wf.add_trace(
                            go.Scatter(
                                x=daily_workflow["timestamp"],
                                y=daily_workflow["users_full_workflow"],
                                name="Users: Full Workflow",
                                line=dict(color="#17becf", width=3),
                            )
                        )
                        fig_wf.update_layout(
                            title="Daily Unique Users by Workflow Step",
                            height=380,
                            xaxis_title="Time",
                            yaxis_title="Unique Users",
                            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                        )
                        st.plotly_chart(fig_wf, use_container_width=True)

                        # User behavior cuts
                        st.markdown("#### 🧭 User Behavior Cuts (Organic)")
                        user_totals = (
                            workflow_df.groupby("_user_norm")
                            .size()
                            .reset_index(name="organic_traces")
                            .sort_values("organic_traces", ascending=False)
                        )
                        user_totals["intensity_bucket"] = pd.cut(
                            user_totals["organic_traces"],
                            bins=[0, 1, 4, 9, np.inf],
                            labels=["1 trace", "2-4 traces", "5-9 traces", "10+ traces"],
                        )
                        bucket_counts = (
                            user_totals["intensity_bucket"]
                            .value_counts(dropna=False)
                            .reset_index()
                        )
                        bucket_counts.columns = ["Bucket", "Users"]

                        n_steps = len(detected_trace_types)
                        step_counts = (
                            user_day["steps_completed"]
                            .value_counts()
                            .reindex(range(1, n_steps + 1), fill_value=0)
                            .reset_index()
                        )
                        step_counts.columns = ["Steps Completed (same day)", "User-Day Count"]

                        c1, c2 = st.columns(2)
                        with c1:
                            st.markdown("**User intensity distribution (entire period)**")
                            st.dataframe(bucket_counts, use_container_width=True, hide_index=True)
                        with c2:
                            st.markdown("**Step completion distribution (user-days)**")
                            st.dataframe(step_counts, use_container_width=True, hide_index=True)
                    else:
                        st.info("No detected trace types to compute workflow metrics.")
                else:
                    st.info("No valid user emails in organic traces to compute workflow metrics.")
            else:
                st.info("Workflow metrics require `user_email` and `trace_type` fields.")

            # ----- Time series: organic vs planned -----
            ts_freq = freq_map.get(freq, "D")

            total_series = traces_for_org.set_index("_trace_start_utc").resample(ts_freq).size().rename("total_traces")
            organic_series = organic_df.set_index("_trace_start_utc").resample(ts_freq).size().rename("organic_traces")
            planned_series = planned_df.set_index("_trace_start_utc").resample(ts_freq).size().rename("planned_traces")

            org_ts = (
                pd.concat([total_series, organic_series, planned_series], axis=1)
                .fillna(0)
                .reset_index()
                .rename(columns={"_trace_start_utc": "timestamp"})
            )
            org_ts[["total_traces", "organic_traces", "planned_traces"]] = org_ts[
                ["total_traces", "organic_traces", "planned_traces"]
            ].astype(int)

            fig_org = go.Figure()
            fig_org.add_trace(go.Scatter(
                x=org_ts["timestamp"], y=org_ts["organic_traces"],
                name="Organic Traces", line=dict(color="#2ca02c"),
            ))
            fig_org.add_trace(go.Scatter(
                x=org_ts["timestamp"], y=org_ts["total_traces"],
                name="Total Traces", line=dict(color="#1f77b4"), opacity=0.5,
            ))
            fig_org.add_trace(go.Bar(
                x=org_ts["timestamp"], y=org_ts["planned_traces"],
                name="Planned (meeting) Traces", marker_color="#ff7f0e", opacity=0.25,
            ))
            fig_org.update_layout(
                title="Organic vs Planned Usage Over Time",
                barmode="overlay",
                height=450,
                xaxis_title="Time",
                yaxis_title="Trace Count",
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            )
            st.plotly_chart(fig_org, use_container_width=True)

            # ----- Usage by Location -----
            st.subheader("🏢 Usage by Location")
            st.caption(
                "Total organic usage grouped by location. Without config, users are grouped by email domain. "
                "Use the expander below to map domains to friendly names."
            )

            with st.expander("Configure location domain mapping", expanded=False):
                st.markdown(
                    "Map email domains to friendly location names (one per line, format: `domain = Name`). "
                    "Example:\n```\nexample.com = HQ Office\npartner.org = Partner Site\n```"
                )
                # Pre-populate from config locations
                config_loc_lines = []
                for loc in CONFIG.locations:
                    for d in loc.domains:
                        config_loc_lines.append(f"{d} = {loc.name}")
                st.text_area(
                    "Domain mapping",
                    value="\n".join(config_loc_lines),
                    height=120,
                    key="_location_domain_map",
                    placeholder="example.com = New York Office\npartner.org = Partner",
                )

            # Rebuild domain_map after the text_area is rendered
            domain_map = _build_location_mapping_from_session()

            if "user_email" not in organic_df.columns:
                st.info("No user email field available to compute location-based usage.")
            else:
                organic_df_loc = organic_df.copy()

                if domain_map or CONFIG.locations:
                    # Use configured + session-state mapping
                    organic_df_loc["pilot_location"] = organic_df_loc["user_email"].apply(
                        lambda email: _get_location_for_email_dynamic(email, CONFIG, cohort_email_sets, domain_map)
                    )
                else:
                    # No config at all — auto-group by email domain
                    organic_df_loc["pilot_location"] = organic_df_loc["user_email"].apply(_domain_from_email)

                location_names = sorted(organic_df_loc["pilot_location"].unique().tolist())

                # Summary metrics
                total_organic = len(organic_df_loc)
                loc_metric_cols = st.columns(min(len(location_names) + 1, 7))
                loc_metric_cols[0].metric("Total Organic", f"{total_organic:,}")
                for i, loc_name in enumerate(location_names):
                    loc_count = len(organic_df_loc[organic_df_loc["pilot_location"] == loc_name])
                    pct_str = f"{loc_count / total_organic * 100:.0f}%" if total_organic else "0%"
                    loc_metric_cols[(i + 1) % len(loc_metric_cols)].metric(
                        loc_name, f"{loc_count:,}", pct_str,
                    )

                # Time series by location
                loc_series_list = []
                for loc_name in location_names:
                    s = (
                        organic_df_loc[organic_df_loc["pilot_location"] == loc_name]
                        .set_index("_trace_start_utc")
                        .resample(ts_freq)
                        .size()
                        .rename(loc_name)
                    )
                    loc_series_list.append(s)

                if loc_series_list:
                    location_ts = (
                        pd.concat(loc_series_list, axis=1)
                        .fillna(0)
                        .astype(int)
                        .reset_index()
                        .rename(columns={"_trace_start_utc": "timestamp"})
                    )

                    fig_loc = go.Figure()
                    for i, loc_name in enumerate(location_names):
                        if loc_name in ("Other", "Unknown", "unknown"):
                            fig_loc.add_trace(go.Scatter(
                                x=location_ts["timestamp"],
                                y=location_ts[loc_name],
                                name=loc_name,
                                line=dict(color="#7f7f7f", width=1, dash="dot"),
                                opacity=0.5,
                            ))
                        else:
                            fig_loc.add_trace(go.Scatter(
                                x=location_ts["timestamp"],
                                y=location_ts[loc_name],
                                name=loc_name,
                                line=dict(
                                    color=DEFAULT_COLORS[i % len(DEFAULT_COLORS)],
                                    width=3,
                                ),
                                mode="lines+markers",
                            ))
                    fig_loc.update_layout(
                        title="Organic Usage Over Time by Location",
                        height=450,
                        xaxis_title="Time",
                        yaxis_title="Trace Count",
                        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                    )
                    st.plotly_chart(fig_loc, use_container_width=True)

                # Show Other / Unknown details
                other_unknown_count = len(
                    organic_df_loc[organic_df_loc["pilot_location"].isin(["Other", "Unknown", "unknown"])]
                )
                if other_unknown_count > 0:
                    with st.expander(
                        f"{other_unknown_count} traces in Other/Unknown (click to see details)",
                        expanded=False,
                    ):
                        other_emails = (
                            organic_df_loc[
                                organic_df_loc["pilot_location"].isin(["Other", "Unknown", "unknown"])
                            ]["user_email"]
                            .value_counts()
                            .reset_index()
                        )
                        other_emails.columns = ["Email", "Trace Count"]
                        st.dataframe(other_emails, use_container_width=True, hide_index=True)
                        st.caption("These are organic traces not matching any configured location domain or cohort.")
                        domain_counts = (
                            other_emails["Email"]
                            .str.extract(r"@(.+)$")[0]
                            .fillna("unknown")
                            .value_counts()
                            .reset_index()
                        )
                        domain_counts.columns = ["Domain", "Trace Count"]
                        st.dataframe(domain_counts, use_container_width=True, hide_index=True)

            st.divider()

            # ----- Organic usage by cohort (always shown) -----
            st.subheader("🌿 Organic Usage by Cohort")
            st.caption(
                "Define user cohorts to track adoption over time. "
                "Enter cohort names and emails below, or pre-populate them via config.yaml."
            )

            # Build default text from config cohorts
            config_cohort_lines = []
            for cohort in CONFIG.cohorts:
                config_cohort_lines.append(cohort.name)
                for e in cohort.emails:
                    config_cohort_lines.append(e)
                config_cohort_lines.append("")  # blank line separator

            cohort_instructions = (
                "Enter cohorts in this format — cohort name on its own line, "
                "then emails below it, separated by a blank line between cohorts:\n\n"
                "```\nTeam Alpha\nalice@co.com\nbob@co.com\n\nTeam Beta\ncharlie@co.com\n```"
            )
            st.markdown(cohort_instructions)

            cohort_def_text = st.text_area(
                "Cohort definitions",
                value="\n".join(config_cohort_lines).strip(),
                height=200,
                key="_cohort_definitions",
                placeholder="Team Alpha\nalice@company.com\nbob@company.com\n\nTeam Beta\ncharlie@company.com",
            )

            # Rebuild cohort sets after the text_area is rendered
            ui_cohort_sets = _parse_cohort_definitions(cohort_def_text)

            # Merge with config cohorts
            merged_cohort_sets: dict = {}
            for cohort in CONFIG.cohorts:
                merged_cohort_sets[cohort.name] = {e.lower().strip() for e in cohort.emails}
            for name, emails in ui_cohort_sets.items():
                if name in merged_cohort_sets:
                    merged_cohort_sets[name] = merged_cohort_sets[name] | emails
                else:
                    merged_cohort_sets[name] = emails

            # Update the shared cohort_email_sets for downstream
            cohort_email_sets.update(merged_cohort_sets)

            if not merged_cohort_sets:
                st.info(
                    "No cohorts defined yet. Enter cohort names and emails above "
                    "to see adoption tracking charts."
                )
            elif "user_email" not in organic_df.columns:
                st.info("No user email field available to compute cohort organic usage.")
            else:
                cohort_names = list(merged_cohort_sets.keys())
                st.markdown(f"**Tracking cohorts:** {', '.join(cohort_names)}")

                org_emails = organic_df["user_email"].astype(str).str.lower().str.strip()

                # Build cohort series dynamically
                cohort_series_list = []
                for cohort_name, cohort_set in merged_cohort_sets.items():
                    # Also match by configured location domains
                    domain_mask = pd.Series(False, index=organic_df.index)
                    for loc in CONFIG.locations:
                        if cohort_name in loc.cohort_names:
                            for loc_domain in loc.domains:
                                domain_mask = domain_mask | org_emails.str.contains(loc_domain, na=False)
                    match_mask = org_emails.isin(cohort_set) | domain_mask

                    s = (
                        organic_df.loc[match_mask]
                        .set_index("_trace_start_utc")
                        .resample(ts_freq)
                        .size()
                        .rename(cohort_name)
                    )
                    cohort_series_list.append(s)

                if cohort_series_list:
                    cohort_ts = (
                        pd.concat(cohort_series_list, axis=1)
                        .fillna(0)
                        .astype(int)
                        .reset_index()
                        .rename(columns={"_trace_start_utc": "timestamp"})
                    )

                    fig_coh = go.Figure()
                    for i, cohort_name in enumerate(cohort_names):
                        # Try to get color from config cohort
                        config_cohort = next((c for c in CONFIG.cohorts if c.name == cohort_name), None)
                        color = _cohort_color(i, config_cohort) if config_cohort else DEFAULT_COLORS[i % len(DEFAULT_COLORS)]
                        fig_coh.add_trace(go.Scatter(
                            x=cohort_ts["timestamp"],
                            y=cohort_ts[cohort_name],
                            name=f"{cohort_name} Organic",
                            line=dict(color=color),
                        ))
                    fig_coh.update_layout(
                        title=f"Organic Usage Over Time — Cohorts ({', '.join(cohort_names)})",
                        height=420,
                        xaxis_title="Time",
                        yaxis_title="Trace Count",
                        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                    )
                    st.plotly_chart(fig_coh, use_container_width=True)

                # Show unmatched organic traces
                all_cohort_email_set = set()
                for s in merged_cohort_sets.values():
                    all_cohort_email_set.update(s)
                # Also include domain-based matching
                all_domain_mask = pd.Series(False, index=organic_df.index)
                for loc in CONFIG.locations:
                    for loc_domain in loc.domains:
                        all_domain_mask = all_domain_mask | org_emails.str.contains(loc_domain, na=False)
                unmatched_mask = ~(org_emails.isin(all_cohort_email_set) | all_domain_mask)
                unmatched_organic = organic_df[unmatched_mask].copy()

                if not unmatched_organic.empty:
                    with st.expander(
                        f"{len(unmatched_organic)} organic traces NOT in any cohort (click to investigate)",
                        expanded=False,
                    ):
                        st.markdown("These organic traces have user emails that don't match any cohort or location domain:")
                        unmatched_emails = unmatched_organic["user_email"].value_counts().reset_index()
                        unmatched_emails.columns = ["Email", "Trace Count"]
                        st.dataframe(unmatched_emails.head(20), use_container_width=True, hide_index=True)

                        st.markdown("**Possible causes:**")
                        st.markdown(
                            "- Email extracted in different format than cohort list\n"
                            "- User not added to any cohort list yet\n"
                            "- Email showing as 'unknown_xxx' (extraction failed)"
                        )

    # ==================================================================
    # TAB 3: Usage Report
    # ==================================================================
    with tab3:
        st.header("📋 Comprehensive Usage Report")
        st.markdown("*Detailed breakdown by user level, resource type, and geography*")

        # ----- Cohort configuration (inline UI) -----
        st.subheader("👥 Cohort Configuration")
        st.caption(
            "Define cohorts here (or reuse those from the Usage Analytics tab). "
            "Format: cohort name on its own line, emails below, blank line between cohorts."
        )

        # Pre-populate from session state (shared with tab 2) or config
        report_cohort_default = st.session_state.get("_cohort_definitions", "")
        if not report_cohort_default:
            lines = []
            for cohort in CONFIG.cohorts:
                lines.append(cohort.name)
                for e in cohort.emails:
                    lines.append(e)
                lines.append("")
            report_cohort_default = "\n".join(lines).strip()

        report_cohort_text = st.text_area(
            "Cohort definitions (for this report)",
            value=report_cohort_default,
            height=180,
            key="report_cohort_defs",
            placeholder="Team Alpha\nalice@company.com\nbob@company.com\n\nTeam Beta\ncharlie@company.com",
        )

        report_cohort_sets = _parse_cohort_definitions(report_cohort_text)
        # Merge with config cohorts
        for cohort in CONFIG.cohorts:
            cname = cohort.name
            if cname in report_cohort_sets:
                report_cohort_sets[cname] = report_cohort_sets[cname] | {e.lower().strip() for e in cohort.emails}
            else:
                report_cohort_sets[cname] = {e.lower().strip() for e in cohort.emails}

        cohort_email_lists = {name: sorted(emails) for name, emails in report_cohort_sets.items()}

        # Auto-discover users by location domain
        if cohort_email_lists and not analyzer.traces_df.empty and "user_email" in analyzer.traces_df.columns:
            active_emails_lower = (
                analyzer.traces_df["user_email"].astype(str).str.lower().str.strip()
            )
            for loc in CONFIG.locations:
                for cohort_name in loc.cohort_names:
                    if cohort_name in cohort_email_lists:
                        for loc_domain in loc.domains:
                            auto_emails = sorted({
                                e for e in active_emails_lower.unique().tolist()
                                if loc_domain in e and e and e != "unknown" and not e.startswith("unknown_")
                            })
                            cohort_email_lists[cohort_name] = sorted(
                                set(cohort_email_lists[cohort_name]) | set(auto_emails)
                            )

        # Generate report data
        usage_breakdown = analyzer.get_usage_breakdown_by_level()
        resource_categories = analyzer.get_resource_category_breakdown()
        geo_data = analyzer.extract_zip_codes()

        # Overall metrics — dynamic
        st.subheader("Total Usage Summary")
        total_users = (
            len(usage_breakdown["high"])
            + len(usage_breakdown["medium"])
            + len(usage_breakdown["low"])
        )

        # Build metrics row dynamically
        metric_items = [("Unique Users", total_users), ("Total Traces", len(analyzer.traces_df))]
        for ttype in detected_trace_types:
            count = len(analyzer.traces_df[analyzer.traces_df["trace_type"] == ttype])
            label = ttype.replace("_", " ").title()
            metric_items.append((label, count))

        metric_cols = st.columns(min(len(metric_items), 6))
        for i, (label, value) in enumerate(metric_items):
            with metric_cols[i % len(metric_cols)]:
                st.metric(label, f"{value:,}" if isinstance(value, int) else value)

        st.divider()

        # User breakdown by level
        st.subheader("🧑‍💼 User Breakdown by Usage Level")

        if usage_breakdown["high"]:
            st.markdown("### High-Usage Staff (4+ traces)")
            high_df = pd.DataFrame(usage_breakdown["high"])
            high_df.columns = ["Email", "Name", "Total Traces"]
            st.dataframe(high_df, use_container_width=True, hide_index=True)

        if usage_breakdown["medium"]:
            st.markdown("### Medium-Usage Staff (2-3 traces)")
            medium_df = pd.DataFrame(usage_breakdown["medium"])
            medium_df.columns = ["Email", "Name", "Total Traces"]
            st.dataframe(medium_df, use_container_width=True, hide_index=True)

        if usage_breakdown["low"]:
            st.markdown("### Low-Usage Staff (1 trace)")
            low_df = pd.DataFrame(usage_breakdown["low"])
            low_df.columns = ["Email", "Name", "Total Traces"]
            st.dataframe(low_df, use_container_width=True, hide_index=True)

        st.divider()

        # Cohort Analysis — always shown
        st.subheader("👥 User Cohort Analysis")

        if not cohort_email_lists:
            st.info(
                "No cohorts defined. Enter cohort definitions above to see adoption tracking. "
                "You can also define them in config.yaml."
            )
        else:
            tab_names = ["📊 Overview"] + list(cohort_email_lists.keys())
            cohort_display_tabs = st.tabs(tab_names)

            # Get all cohort analyses
            cohort_analyses = {}
            for cohort_name, emails in cohort_email_lists.items():
                if emails:
                    cohort_analyses[cohort_name] = analyzer.get_cohort_analysis(emails, cohort_name)

            # Overview Tab
            with cohort_display_tabs[0]:
                st.markdown("### 📊 Cohort Adoption Summary")

                overview_cols = st.columns(min(len(cohort_email_lists), 6))
                for i, cohort_name in enumerate(cohort_email_lists.keys()):
                    with overview_cols[i % len(overview_cols)]:
                        data = cohort_analyses.get(cohort_name, {})
                        rate = data.get("adoption_rate", 0) if data else 0
                        st.metric(
                            f"{cohort_name} Adoption",
                            f"{rate:.0f}%",
                            f"{data.get('active_count', 0)}/{data.get('total_cohort', 0)}",
                        )

                # Combined activity table
                st.markdown("### 📈 All Active Users Across Cohorts")
                all_active = []
                for cohort_name in cohort_email_lists.keys():
                    data = cohort_analyses.get(cohort_name, {})
                    if data and data.get("active_users"):
                        for user in data["active_users"]:
                            user_copy = user.copy()
                            user_copy["cohort"] = cohort_name
                            all_active.append(user_copy)

                if all_active:
                    all_active_df = pd.DataFrame(all_active)
                    display_cols = ["cohort", "name", "email", "trace_count", "first_trace", "last_trace"]
                    available_cols = [c for c in display_cols if c in all_active_df.columns]
                    all_active_display = all_active_df[available_cols].copy()
                    friendly = ["Cohort", "Name", "Email", "Traces", "First Use", "Last Use"]
                    all_active_display.columns = friendly[: len(available_cols)]
                    if "First Use" in all_active_display.columns:
                        all_active_display["First Use"] = pd.to_datetime(
                            all_active_display["First Use"]
                        ).dt.strftime("%Y-%m-%d")
                    if "Last Use" in all_active_display.columns:
                        all_active_display["Last Use"] = pd.to_datetime(
                            all_active_display["Last Use"]
                        ).dt.strftime("%Y-%m-%d")
                    all_active_display = all_active_display.sort_values("Traces", ascending=False)
                    st.dataframe(all_active_display, use_container_width=True, hide_index=True)
                else:
                    st.info("No active users found across any cohort")

            # Helper function to display cohort details
            def display_cohort(cohort_data, cohort_name):
                if not cohort_data:
                    st.info(f"No {cohort_name} cohort data configured")
                    return

                col1, col2, col3 = st.columns(3)
                with col1:
                    st.metric(f"Total {cohort_name} Users", cohort_data.get("total_cohort", 0))
                with col2:
                    st.metric("Active Users", cohort_data.get("active_count", 0))
                with col3:
                    adoption = cohort_data.get("adoption_rate", 0)
                    st.metric("Adoption Rate", f"{adoption:.1f}%")

                if cohort_data.get("active_users"):
                    st.markdown(f"### {cohort_name} Users Who HAVE Used the Tool")
                    active_df = pd.DataFrame(cohort_data["active_users"])
                    display_cols = ["name", "email", "trace_count", "first_trace", "last_trace"]
                    available_cols = [c for c in display_cols if c in active_df.columns]
                    active_display = active_df[available_cols].copy()
                    active_display.columns = ["Name", "Email", "Traces", "First Use", "Last Use"][: len(available_cols)]
                    if "First Use" in active_display.columns:
                        active_display["First Use"] = pd.to_datetime(active_display["First Use"]).dt.strftime("%Y-%m-%d")
                    if "Last Use" in active_display.columns:
                        active_display["Last Use"] = pd.to_datetime(active_display["Last Use"]).dt.strftime("%Y-%m-%d")
                    st.dataframe(active_display, use_container_width=True, hide_index=True)

                if cohort_data.get("non_users"):
                    st.markdown(f"### {cohort_name} Users Who Have NOT Used the Tool")
                    non_users_df = pd.DataFrame(cohort_data["non_users"])
                    non_users_display = non_users_df[["name", "email"]].copy()
                    non_users_display.columns = ["Name", "Email"]
                    st.dataframe(non_users_display, use_container_width=True, hide_index=True)

            # Individual cohort tabs
            for i, cohort_name in enumerate(cohort_email_lists.keys()):
                with cohort_display_tabs[i + 1]:
                    st.markdown(f"### {cohort_name}")
                    display_cohort(cohort_analyses.get(cohort_name, {}), cohort_name)

        st.divider()

        # Resource category breakdown
        st.subheader("🎯 Resource Type Demand")

        if resource_categories:
            cat_summary = []
            for category, data in resource_categories.items():
                cat_summary.append({
                    "Resource Type": category,
                    "Count": data["count"],
                    "% of Total": f"{data['percentage']:.1f}%",
                })
            cat_df = pd.DataFrame(cat_summary)
            st.dataframe(cat_df, use_container_width=True, hide_index=True)

            fig = px.bar(
                cat_df,
                x="Resource Type",
                y="Count",
                title="Resource Demand by Category",
                text="Count",
            )
            fig.update_traces(textposition="outside")
            fig.update_layout(xaxis_tickangle=-45)
            st.plotly_chart(fig, use_container_width=True)

            st.markdown("### 🧭 Category Breakdown with Examples")
            for category, data in resource_categories.items():
                with st.expander(f"{category} — {data['count']} traces ({data['percentage']:.1f}%)"):
                    if data.get("examples"):
                        st.markdown("**Sample queries:**")
                        for i, example in enumerate(data["examples"], 1):
                            st.markdown(f"{i}. **{example['user']}**: {example['query']}")

        st.divider()

        # Geographic analysis
        st.subheader("📍 Geographic Coverage")

        if geo_data.get("all_zips"):
            # Summary metrics
            col_m1, col_m2 = st.columns(2)
            with col_m1:
                st.metric("Total Unique Zip Codes", geo_data.get("total_unique", 0))
            with col_m2:
                st.metric("Total Zip References", geo_data.get("total_references", 0))

            col1, col2 = st.columns(2)

            # Top zip codes table
            with col1:
                st.markdown("**Top Zip Codes (by frequency)**")
                top_zips = geo_data["all_zips"][:20]
                top_zips_df = pd.DataFrame(top_zips)
                if not top_zips_df.empty:
                    top_zips_df.columns = [c.title() for c in top_zips_df.columns]
                    st.dataframe(top_zips_df, use_container_width=True, hide_index=True)

            # Usage by City / Region table
            with col2:
                by_city = geo_data.get("by_city", {})
                by_prefix = geo_data.get("by_prefix", {})

                # Prefer city grouping; fall back to prefix grouping
                has_real_cities = by_city and any(
                    k != "Unknown" for k in by_city.keys()
                )
                if has_real_cities:
                    st.markdown("**Usage by City**")
                    city_rows = []
                    for city_name, info in sorted(
                        by_city.items(), key=lambda x: x[1]["count"], reverse=True
                    ):
                        city_rows.append({
                            "City": city_name,
                            "Unique Zips": len(info["zips"]),
                            "Trace Count": info["count"],
                        })
                    city_df = pd.DataFrame(city_rows)
                    st.dataframe(city_df, use_container_width=True, hide_index=True)
                elif by_prefix:
                    st.markdown("**Usage by Zip Prefix (metro area estimate)**")
                    prefix_rows = []
                    for prefix, info in sorted(
                        by_prefix.items(), key=lambda x: x[1]["count"], reverse=True
                    ):
                        prefix_rows.append({
                            "Zip Prefix": prefix + "xx",
                            "Unique Zips": len(info["zips"]),
                            "Trace Count": info["count"],
                        })
                    prefix_df = pd.DataFrame(prefix_rows)
                    st.dataframe(prefix_df, use_container_width=True, hide_index=True)
                else:
                    st.info("No city mapping available. Add zip_to_city in config for city-level grouping.")
        else:
            st.info("No zip codes detected in trace queries")

        # Download report
        st.divider()
        st.subheader("📥 Export Report")

        col1, col2 = st.columns(2)
        with col1:
            if usage_breakdown["high"]:
                csv = high_df.to_csv(index=False).encode("utf-8")
                st.download_button(
                    "Download High-Usage Users CSV",
                    csv,
                    "high_usage_users.csv",
                    "text/csv",
                )
        with col2:
            if resource_categories:
                cat_csv = cat_df.to_csv(index=False).encode("utf-8")
                st.download_button(
                    "Download Resource Categories CSV",
                    cat_csv,
                    "resource_categories.csv",
                    "text/csv",
                )

    # ==================================================================
    # TAB 4: Performance Metrics
    # ==================================================================
    with tab4:
        st.header("Performance Metrics")
        st.markdown("*Detailed performance analysis*")

        user_facing_traces = (
            analyzer.get_user_facing_traces()
            if hasattr(analyzer, "get_user_facing_traces")
            else pd.DataFrame()
        )

        # Latency distribution
        st.subheader("Latency Distribution")
        latency_dist = analyzer.get_latency_distribution()

        if latency_dist:
            all_values = np.array(latency_dist["histogram"])
            p95 = np.percentile(all_values, 95)
            p99 = np.percentile(all_values, 99)

            # Determine outlier threshold using IQR method, capped at p99
            q1 = np.percentile(all_values, 25)
            q3 = np.percentile(all_values, 75)
            iqr = q3 - q1
            iqr_upper = q3 + 3.0 * iqr  # 3x IQR for "far" outliers
            clip_threshold = min(iqr_upper, p99) if iqr_upper > 0 else p99
            # Ensure the threshold is at least p95
            clip_threshold = max(clip_threshold, p95)

            clipped_values = all_values[all_values <= clip_threshold]
            n_outliers = int(len(all_values) - len(clipped_values))

            # Compute clean stats (excluding outliers)
            clean_mean = float(np.mean(clipped_values)) if len(clipped_values) > 0 else 0.0

            # Check if Max is likely anomalous
            full_max = latency_dist["max"]
            max_anomalous = full_max > 10 * p95

            use_log = st.checkbox("Log scale (x-axis)", value=False, key="latency_log_scale")

            col1, col2 = st.columns([2, 1])

            with col1:
                if use_log:
                    # Log scale: show all data, log x-axis naturally spreads the tail
                    plot_values = all_values[all_values > 0]
                    fig = px.histogram(
                        x=plot_values,
                        nbins=40,
                        title="Latency Distribution (log scale)",
                        labels={"x": "Latency (s)", "y": "Count"},
                        log_x=True,
                    )
                    st.plotly_chart(fig, use_container_width=True)
                else:
                    fig = px.histogram(
                        x=clipped_values,
                        nbins=40,
                        title="Latency Distribution",
                        labels={"x": "Latency (s)", "y": "Count"},
                    )
                    st.plotly_chart(fig, use_container_width=True)
                    if n_outliers > 0:
                        st.caption(
                            f"Showing distribution up to {clip_threshold:.1f}s. "
                            f"{n_outliers} outlier(s) above this threshold excluded from chart."
                        )

            with col2:
                st.markdown("**Latency Statistics (full data)**")
                if "population" in latency_dist:
                    st.caption(
                        f"Population: `{latency_dist['population']}` "
                        "(prefers real user questions when available)"
                    )
                st.metric("Mean", f"{latency_dist['mean']:.2f}s")
                if n_outliers > 0:
                    st.metric("Mean (excl. outliers)", f"{clean_mean:.2f}s")
                st.metric("Median", f"{latency_dist['median']:.2f}s")
                st.metric("Std Dev", f"{latency_dist['std']:.2f}s")
                st.metric("Min", f"{latency_dist['min']:.2f}s")
                if max_anomalous:
                    st.metric("Max", f"{full_max:.2f}s")
                    st.warning(
                        f"Max ({full_max:.0f}s) is >{10}x the p95 ({p95:.1f}s) — likely an anomalous/broken trace."
                    )
                else:
                    st.metric("Max", f"{full_max:.2f}s")

                if n_outliers > 0:
                    st.markdown("---")
                    st.markdown(f"**Outliers:** {n_outliers} traces above {clip_threshold:.1f}s")

        # Percentiles
        if latency_dist and "percentiles" in latency_dist:
            st.subheader("Latency Percentiles")
            percentiles_df = pd.DataFrame(
                list(latency_dist["percentiles"].items()),
                columns=["Percentile", "Latency (s)"],
            )
            fig = px.bar(percentiles_df, x="Percentile", y="Latency (s)", title="Latency Percentiles")
            st.plotly_chart(fig, use_container_width=True)

        st.divider()
        st.subheader("Latency Over Time (Percentiles)")
        st.caption("Percentiles are usually the best way to track perceived speed (tail latency).")

        perf_freq = st.selectbox(
            "Time Granularity",
            list(freq_map.keys()),
            index=1,
            key="perf_latency_granularity",
        )

        ts_df = user_facing_traces.copy() if not user_facing_traces.empty else analyzer.traces_df.copy()
        if ts_df is None or ts_df.empty:
            st.info("No trace data available for latency trends.")
        else:
            if "trace_start" in ts_df.columns:
                ts_df["trace_start"] = pd.to_datetime(ts_df["trace_start"], errors="coerce", utc=True)
            if "trace_duration_s" in ts_df.columns:
                ts_df["trace_duration_s"] = pd.to_numeric(ts_df["trace_duration_s"], errors="coerce")

            ts_df = ts_df.dropna(subset=[c for c in ["trace_start", "trace_duration_s"] if c in ts_df.columns])
            ts_df = ts_df[ts_df["trace_duration_s"] > 0] if "trace_duration_s" in ts_df.columns else ts_df

            if ts_df.empty or "trace_start" not in ts_df.columns or "trace_duration_s" not in ts_df.columns:
                st.info("Not enough data to plot latency percentiles over time.")
            else:
                freq_code = freq_map.get(perf_freq, "D")
                grouped = ts_df.set_index("trace_start").resample(freq_code)["trace_duration_s"]
                latency_over_time = pd.DataFrame({
                    "p50": grouped.quantile(0.50),
                    "p90": grouped.quantile(0.90),
                    "p95": grouped.quantile(0.95),
                    "count": grouped.size(),
                }).reset_index().rename(columns={"trace_start": "timestamp"})
                latency_over_time = latency_over_time.dropna(subset=["timestamp"])

                fig = go.Figure()
                fig.add_trace(go.Scatter(
                    x=latency_over_time["timestamp"], y=latency_over_time["p50"],
                    name="p50", line=dict(color="#1f77b4"),
                ))
                fig.add_trace(go.Scatter(
                    x=latency_over_time["timestamp"], y=latency_over_time["p90"],
                    name="p90", line=dict(color="#ff7f0e"),
                ))
                fig.add_trace(go.Scatter(
                    x=latency_over_time["timestamp"], y=latency_over_time["p95"],
                    name="p95", line=dict(color="#d62728"),
                ))
                _percentile_y_cap = 2 * _latency_p95 if _latency_p95 > 0 else 60
                fig.update_layout(
                    height=420,
                    xaxis_title="Time",
                    yaxis_title="Latency (s)",
                    title="Latency Percentiles Over Time",
                    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                    yaxis_range=[0, _percentile_y_cap],
                )
                st.plotly_chart(fig, use_container_width=True)

                st.caption("Trace counts per bucket help interpret percentiles (low volume buckets can be noisy).")
                fig_count = px.bar(
                    latency_over_time,
                    x="timestamp",
                    y="count",
                    title="Trace Volume Over Time",
                    labels={"count": "Traces", "timestamp": "Time"},
                )
                st.plotly_chart(fig_count, use_container_width=True)

        st.divider()
        st.subheader("Tail Latency Volume (Thresholds)")
        st.caption("Shows how often users experience very slow responses.")

        thresholds = st.multiselect(
            "Latency thresholds (seconds)",
            options=[5, 10, 20, 30, 60, 120],
            default=[10, 30, 60],
            key="perf_tail_thresholds",
        )
        if ts_df is None or ts_df.empty or "trace_start" not in ts_df.columns or "trace_duration_s" not in ts_df.columns:
            st.info("Not enough data to compute tail thresholds.")
        else:
            if not thresholds:
                st.info("Select at least one threshold.")
            else:
                freq_code = freq_map.get(perf_freq, "D")
                tail = ts_df[["trace_start", "trace_duration_s"]].copy()
                tail = tail.set_index("trace_start")
                out = pd.DataFrame(index=tail.resample(freq_code).size().index)
                out.index.name = "timestamp"
                for t in thresholds:
                    out[f">{t}s"] = tail["trace_duration_s"].resample(freq_code).apply(
                        lambda s, _t=t: int((s > _t).sum())
                    )
                out = out.reset_index()
                long_df = out.melt(id_vars=["timestamp"], var_name="threshold", value_name="slow_traces")
                fig = px.line(
                    long_df,
                    x="timestamp",
                    y="slow_traces",
                    color="threshold",
                    title="Slow Trace Counts Over Time",
                    labels={"slow_traces": "Slow traces", "timestamp": "Time"},
                )
                st.plotly_chart(fig, use_container_width=True)

        st.divider()
        st.subheader("Latency by Trace Type")
        st.caption("Useful for spotting which workflow is slowest.")

        by_type = user_facing_traces.copy() if not user_facing_traces.empty else analyzer.traces_df.copy()
        if by_type is None or by_type.empty or "trace_type" not in by_type.columns or "trace_duration_s" not in by_type.columns:
            st.info("Not enough data to plot latency by trace type.")
        else:
            by_type["trace_duration_s"] = pd.to_numeric(by_type["trace_duration_s"], errors="coerce")
            by_type = by_type.dropna(subset=["trace_duration_s"])
            by_type = by_type[by_type["trace_duration_s"] > 0]
            _n_before_cap = len(by_type)
            by_type = by_type[by_type["trace_duration_s"] <= _outlier_cap]
            _n_filtered = _n_before_cap - len(by_type)
            if _n_filtered > 0:
                st.caption(f"Filtered {_n_filtered} outlier trace(s) above {_outlier_cap:.1f}s for chart clarity.")
            fig = px.box(
                by_type,
                x="trace_type",
                y="trace_duration_s",
                points="outliers",
                title="Latency Distribution by Trace Type",
                labels={"trace_duration_s": "Latency (s)", "trace_type": "Trace Type"},
            )
            st.plotly_chart(fig, use_container_width=True)

        st.divider()
        st.subheader("Slowest Users (by p95)")
        st.caption("Helps identify user segments that consistently experience slowness.")

        by_user = user_facing_traces.copy() if not user_facing_traces.empty else analyzer.traces_df.copy()
        if by_user is None or by_user.empty or "user_email" not in by_user.columns or "trace_duration_s" not in by_user.columns:
            st.info("Not enough data to compute slowest users.")
        else:
            by_user["trace_duration_s"] = pd.to_numeric(by_user["trace_duration_s"], errors="coerce")
            by_user = by_user.dropna(subset=["trace_duration_s"])
            by_user = by_user[by_user["trace_duration_s"] > 0]

            min_traces_per_user = st.slider(
                "Minimum traces per user",
                min_value=1, max_value=50, value=5, step=1,
                key="perf_min_traces_user",
            )
            user_stats = (
                by_user.groupby("user_email", as_index=False)
                .agg(
                    traces=("trace_id", "count") if "trace_id" in by_user.columns else ("trace_duration_s", "count"),
                    avg_latency_s=("trace_duration_s", "mean"),
                    p95_latency_s=("trace_duration_s", lambda x: x.quantile(0.95)),
                )
            )
            user_stats = user_stats[user_stats["traces"] >= min_traces_per_user]
            user_stats = user_stats.sort_values("p95_latency_s", ascending=False).head(25)
            st.dataframe(user_stats, use_container_width=True, hide_index=True)

            fig = px.bar(
                user_stats.sort_values("p95_latency_s", ascending=True),
                x="p95_latency_s",
                y="user_email",
                orientation="h",
                title="Top 25 Slowest Users (p95 latency)",
                labels={"p95_latency_s": "p95 latency (s)", "user_email": "User"},
            )
            st.plotly_chart(fig, use_container_width=True)

        st.divider()
        st.subheader("Top Slow Traces (Downloadable)")
        st.caption("This is the best dataset to hand to engineering for concrete latency debugging.")

        slow = user_facing_traces.copy() if not user_facing_traces.empty else analyzer.traces_df.copy()
        if slow is None or slow.empty or "trace_duration_s" not in slow.columns:
            st.info("No trace duration data available.")
        else:
            slow["trace_duration_s"] = pd.to_numeric(slow["trace_duration_s"], errors="coerce")
            slow = slow.dropna(subset=["trace_duration_s"])
            slow = slow[slow["trace_duration_s"] > 0]

            include_errors = st.checkbox("Include ERROR traces", value=False, key="perf_include_error_traces")
            if not include_errors and "status" in slow.columns:
                slow = slow[slow["status"] != "ERROR"]

            trace_types_avail = sorted(slow["trace_type"].dropna().unique().tolist()) if "trace_type" in slow.columns else []
            selected_types = st.multiselect(
                "Trace types",
                options=trace_types_avail,
                default=trace_types_avail,
                key="perf_trace_types",
            )
            if selected_types and "trace_type" in slow.columns:
                slow = slow[slow["trace_type"].isin(selected_types)]

            top_n = st.slider("How many slow traces to show", min_value=10, max_value=500, value=100, step=10, key="perf_top_n_traces")
            slow = slow.sort_values("trace_duration_s", ascending=False).head(top_n)

            cols = [c for c in [
                "trace_start", "trace_id", "user_email", "user_name", "trace_type",
                "trace_duration_s", "total_tokens", "status", "query", "category",
                "location_preference", "zip_code",
            ] if c in slow.columns]
            slow_view = slow[cols].copy()
            st.dataframe(slow_view, use_container_width=True, hide_index=True)

            st.download_button(
                "📥 Download slow traces (CSV)",
                slow_view.to_csv(index=False).encode("utf-8"),
                file_name="phoenix_slowest_traces.csv",
                mime="text/csv",
                key="download-slowest-traces",
            )

        st.divider()
        st.subheader("Per-Trace Bottleneck Breakdown (Selected Trace)")
        st.caption("Shows which span(s) dominate end-to-end latency for a single slow trace.")

        if slow is None or slow.empty or "trace_id" not in slow.columns:
            st.info("Select a time range with trace IDs available.")
        else:
            trace_id_options = slow["trace_id"].dropna().astype(str).tolist()
            selected_trace_id = st.selectbox(
                "Select a trace_id to inspect",
                options=trace_id_options,
                index=0 if trace_id_options else None,
                key="perf_selected_trace_id",
            )

            if not selected_trace_id:
                st.info("No trace selected.")
            else:
                spans_df = analyzer.df.copy() if hasattr(analyzer, "df") else pd.DataFrame()
                if spans_df is None or spans_df.empty or "trace_id" not in spans_df.columns:
                    st.info("No span-level data available to compute per-trace breakdown.")
                else:
                    trace_spans = spans_df[spans_df["trace_id"].astype(str) == str(selected_trace_id)].copy()
                    if trace_spans.empty:
                        st.info("No spans found for selected trace.")
                    else:
                        trace_spans["start_time"] = pd.to_datetime(trace_spans.get("start_time"), errors="coerce", utc=True)
                        trace_spans["end_time"] = pd.to_datetime(trace_spans.get("end_time"), errors="coerce", utc=True)
                        trace_spans["latency_s"] = pd.to_numeric(trace_spans.get("latency_s"), errors="coerce")
                        trace_spans = trace_spans.dropna(subset=["latency_s"])
                        trace_spans = trace_spans[trace_spans["latency_s"] > 0]
                        trace_spans = trace_spans.sort_values("start_time")

                        trace_total = None
                        if (
                            analyzer.traces_df is not None
                            and not analyzer.traces_df.empty
                            and "trace_duration_s" in analyzer.traces_df.columns
                        ):
                            matching = analyzer.traces_df.loc[
                                analyzer.traces_df["trace_id"].astype(str) == str(selected_trace_id),
                                "trace_duration_s",
                            ]
                            if not matching.empty:
                                trace_total = float(pd.to_numeric(matching.iloc[0], errors="coerce"))

                        view = trace_spans[["name", "latency_s", "start_time", "end_time"]].copy() if "name" in trace_spans.columns else trace_spans[["latency_s", "start_time", "end_time"]].copy()
                        if trace_total and trace_total > 0:
                            view["pct_of_trace"] = (view["latency_s"] / trace_total * 100).round(1)

                        # Warn if this trace looks anomalous
                        if trace_total and trace_total > 10 * _latency_median and _latency_median > 0:
                            st.warning(
                                f"This trace ({trace_total:.1f}s) is >{10}x the median ({_latency_median:.1f}s) "
                                "— it appears anomalous. Span proportions may still be informative."
                            )

                        fig = px.bar(
                            view,
                            x="latency_s",
                            y=view.index.astype(str),
                            orientation="h",
                            title=f"Span durations for trace {selected_trace_id}",
                            labels={"latency_s": "Latency (s)", "y": "Span (execution order)"},
                            hover_data=[c for c in ["name", "pct_of_trace", "start_time", "end_time"] if c in view.columns],
                        )
                        fig.update_layout(height=520, yaxis=dict(showticklabels=False))
                        st.plotly_chart(fig, use_container_width=True)
                        st.dataframe(view.reset_index(drop=True), use_container_width=True, hide_index=True)

        st.divider()
        st.subheader("Where Time Goes (Aggregate Span Contribution)")
        st.caption("Aggregates span latency across user-facing traces to highlight systemic bottlenecks.")

        spans_df = analyzer.df.copy() if hasattr(analyzer, "df") else pd.DataFrame()
        if spans_df is None or spans_df.empty or "latency_s" not in spans_df.columns:
            st.info("No span-level latency data available.")
        else:
            spans_df["latency_s"] = pd.to_numeric(spans_df["latency_s"], errors="coerce")
            spans_df = spans_df.dropna(subset=["latency_s"])
            spans_df = spans_df[spans_df["latency_s"] > 0]

            if user_facing_traces is not None and not user_facing_traces.empty and "trace_id" in user_facing_traces.columns and "trace_id" in spans_df.columns:
                uf_ids = set(user_facing_traces["trace_id"].dropna().astype(str))
                spans_df = spans_df[spans_df["trace_id"].astype(str).isin(uf_ids)]

            if spans_df.empty or "name" not in spans_df.columns:
                st.info("Not enough span data to compute aggregate contribution.")
            else:
                agg = (
                    spans_df.groupby("name", as_index=False)
                    .agg(
                        spans=("span_id", "count") if "span_id" in spans_df.columns else ("latency_s", "count"),
                        avg_latency_s=("latency_s", "mean"),
                        p95_latency_s=("latency_s", lambda x: x.quantile(0.95)),
                        total_span_time_s=("latency_s", "sum"),
                    )
                )
                total_time = float(agg["total_span_time_s"].sum()) if not agg.empty else 0
                if total_time > 0:
                    agg["contribution_pct"] = (agg["total_span_time_s"] / total_time * 100).round(2)
                agg = agg.sort_values("total_span_time_s", ascending=False).head(30)
                st.dataframe(agg, use_container_width=True, hide_index=True)

                fig = px.bar(
                    agg.sort_values("total_span_time_s", ascending=True),
                    x="total_span_time_s",
                    y="name",
                    orientation="h",
                    title="Top span groups by total time (sum of span latencies)",
                    labels={"total_span_time_s": "Total time (s)", "name": "Span name"},
                )
                st.plotly_chart(fig, use_container_width=True)

        # Model comparison
        st.subheader("Model Performance Comparison")
        model_comparison = analyzer.get_model_performance_comparison()

        if not model_comparison.empty:
            st.dataframe(
                model_comparison.style.format({
                    "total_requests": "{:,.0f}",
                    "avg_latency": "{:.2f}s",
                    "median_latency": "{:.2f}s",
                    "avg_tokens": "{:.2f}",
                    "total_tokens": "{:,.0f}",
                    "error_rate": "{:.2f}%",
                }),
                use_container_width=True,
            )

            col1, col2 = st.columns(2)
            with col1:
                fig = px.bar(
                    model_comparison, x="model", y="avg_latency",
                    title="Average Latency by Model",
                    labels={"avg_latency": "Avg Latency (s)"},
                )
                st.plotly_chart(fig, use_container_width=True)
            with col2:
                fig = px.bar(
                    model_comparison, x="model", y="error_rate",
                    title="Error Rate by Model",
                    labels={"error_rate": "Error Rate (%)"},
                )
                st.plotly_chart(fig, use_container_width=True)

        # Error analysis
        st.subheader("Error Analysis")
        error_analysis = analyzer.get_error_analysis()

        if error_analysis and error_analysis["total_errors"] > 0:
            col1, col2 = st.columns(2)
            with col1:
                st.metric("Total Errors", error_analysis["total_errors"])
                st.metric("Error Rate", f"{error_analysis['error_rate']:.2f}%")
            with col2:
                if error_analysis["errors_by_type"]:
                    errors_df = pd.DataFrame(
                        list(error_analysis["errors_by_type"].items()),
                        columns=["Error Type", "Count"],
                    )
                    fig = px.pie(errors_df, values="Count", names="Error Type", title="Errors by Type")
                    st.plotly_chart(fig, use_container_width=True)
        else:
            st.success("No errors detected in the analyzed period!")

    # ==================================================================
    # TAB 5: Log Explorer
    # ==================================================================
    with tab5:
        st.header("Log Explorer")
        st.markdown("*Exportable trace list + latency bottleneck breakdown*")

        if analyzer.traces_df.empty:
            st.info("No trace data available to explore.")
        else:
            st.subheader("📄 All Traces (User, Query, Trace Latency)")
            st.caption("Download-ready dataset of what users asked + how long the end-to-end trace took.")

            only_query_traces = st.checkbox(
                "Only include traces with a non-empty query (recommended for a 'questions' dataset)",
                value=True,
            )

            traces = analyzer.traces_df.copy()
            preferred_cols = [
                "trace_start", "trace_id", "user_name", "user_email",
                "trace_type", "query", "category", "location_preference",
                "zip_code", "trace_duration_s", "status", "total_tokens", "tokens_estimated",
            ]
            cols = [c for c in preferred_cols if c in traces.columns]
            traces = traces[cols].copy()

            if "query" in traces.columns:
                traces["query"] = traces["query"].fillna("").astype(str)
                if only_query_traces:
                    traces = traces[traces["query"].str.strip().str.len() > 0]

            if "trace_start" in traces.columns:
                traces = traces.sort_values("trace_start", ascending=False)

            display = traces.copy()
            if "trace_start" in display.columns:
                display["trace_start"] = pd.to_datetime(display["trace_start"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S")
                display = display.rename(columns={"trace_start": "timestamp"})

            if "trace_duration_s" in display.columns:
                display["trace_duration_s"] = pd.to_numeric(display["trace_duration_s"], errors="coerce").round(3)
                display = display.rename(columns={"trace_duration_s": "trace_latency_s"})

            if "total_tokens" in display.columns:
                display["total_tokens"] = (
                    pd.to_numeric(display["total_tokens"], errors="coerce").fillna(0).astype(int)
                )

            rename_map = {"user_name": "user", "user_email": "email"}
            display = display.rename(columns=rename_map)

            st.dataframe(display.fillna(""), use_container_width=True, hide_index=True)

            csv_bytes = display.to_csv(index=False).encode("utf-8")
            st.download_button(
                "📥 Download trace list (CSV)",
                csv_bytes,
                "phoenix_trace_list.csv",
                "text/csv",
                key="download-trace-list-csv",
            )

            st.divider()
            st.subheader("📈 Trends & Correlation (Latency vs Tokens)")

            trend_df = traces.copy()
            if "trace_start" in trend_df.columns:
                trend_df["trace_start"] = pd.to_datetime(trend_df["trace_start"], errors="coerce")
            if "trace_duration_s" in trend_df.columns:
                trend_df["trace_duration_s"] = pd.to_numeric(trend_df["trace_duration_s"], errors="coerce")
            if "total_tokens" in trend_df.columns:
                trend_df["total_tokens"] = pd.to_numeric(trend_df["total_tokens"], errors="coerce")

            trend_df = trend_df.dropna(subset=[
                c for c in ["trace_start", "trace_duration_s", "total_tokens"] if c in trend_df.columns
            ])

            if trend_df.empty or "trace_start" not in trend_df.columns:
                st.info("Not enough data to plot trends.")
            else:
                trend_df = trend_df.sort_values("trace_start")

                col1, col2 = st.columns(2)
                with col1:
                    fig = px.line(
                        trend_df, x="trace_start", y="trace_duration_s",
                        title="Trace Latency Over Time",
                        labels={"trace_start": "Time", "trace_duration_s": "Latency (s)"},
                    )
                    st.plotly_chart(fig, use_container_width=True)

                with col2:
                    if "total_tokens" in trend_df.columns:
                        fig = px.line(
                            trend_df, x="trace_start", y="total_tokens",
                            title="Tokens Over Time",
                            labels={"trace_start": "Time", "total_tokens": "Tokens"},
                        )
                        st.plotly_chart(fig, use_container_width=True)
                    else:
                        st.info("No token data available for tokens-over-time chart.")

                if "total_tokens" in trend_df.columns:
                    st.subheader("🔎 Latency vs Tokens (Correlation)")
                    corr = trend_df["trace_duration_s"].corr(trend_df["total_tokens"])
                    st.metric("Pearson correlation (latency vs tokens)", f"{corr:.3f}" if pd.notna(corr) else "N/A")

                    fig = px.scatter(
                        trend_df, x="total_tokens", y="trace_duration_s",
                        title="Latency vs Tokens",
                        labels={"total_tokens": "Tokens", "trace_duration_s": "Latency (s)"},
                        hover_data=[c for c in ["user_email", "user_name", "trace_type", "query", "trace_id"] if c in trend_df.columns],
                    )
                    st.plotly_chart(fig, use_container_width=True)

            st.divider()
            st.subheader("⏱️ Average Latency by Trace Step (Span Name)")
            st.caption("Aggregated across the currently listed traces; spot the biggest latency bottlenecks.")

            if analyzer.df.empty or "name" not in analyzer.df.columns:
                st.info("Span-level data is not available for step breakdown.")
            else:
                step_spans_df = analyzer.df.copy()
                if "trace_id" in step_spans_df.columns and "trace_id" in traces.columns:
                    step_spans_df = step_spans_df[step_spans_df["trace_id"].isin(traces["trace_id"].dropna().unique())]

                if "latency_s" in step_spans_df.columns:
                    step_spans_df["latency_s"] = pd.to_numeric(step_spans_df["latency_s"], errors="coerce")
                step_spans_df = step_spans_df.dropna(subset=["name", "latency_s"])

                if step_spans_df.empty:
                    st.info("No spans with latency were found for the selected traces.")
                else:
                    step_df = step_spans_df.groupby("name").agg(
                        avg_latency_s=("latency_s", "mean"),
                        p95_latency_s=("latency_s", lambda x: x.quantile(0.95)),
                        count_spans=("latency_s", "count"),
                        count_traces=("trace_id", pd.Series.nunique) if "trace_id" in step_spans_df.columns else ("latency_s", "count"),
                    ).reset_index().rename(columns={"name": "step"})

                    step_df = step_df.sort_values("avg_latency_s", ascending=False)
                    step_display = step_df.copy()
                    for c in ["avg_latency_s", "p95_latency_s"]:
                        if c in step_display.columns:
                            step_display[c] = pd.to_numeric(step_display[c], errors="coerce").round(3)

                    st.dataframe(step_display, use_container_width=True, hide_index=True)

                    step_csv = step_display.to_csv(index=False).encode("utf-8")
                    st.download_button(
                        "📥 Download step latency breakdown (CSV)",
                        step_csv,
                        "phoenix_span_step_latency.csv",
                        "text/csv",
                        key="download-step-latency-csv",
                    )

    # ==================================================================
    # TAB 6: Advanced Analytics
    # ==================================================================
    with tab6:
        st.header("🧠 Advanced Analytics")
        st.markdown("*Deep insights into query patterns, resource effectiveness, and user behavior*")

        # Feature #1: Query Pattern Analysis
        st.divider()
        st.subheader("🔍 Query Pattern Analysis & Search Intelligence")

        with st.spinner("Analyzing query patterns..."):
            query_analysis = analyzer.analyze_query_patterns()

        if query_analysis:
            col1, col2, col3, col4 = st.columns(4)
            with col1:
                st.metric("Total Unique Queries", query_analysis.get("total_unique_queries", 0))
            with col2:
                st.metric("Avg Query Length", f"{query_analysis.get('avg_query_length', 0):.0f} chars")
            with col3:
                st.metric("Refinement Rate", f"{query_analysis.get('refinement_rate', 0):.1f}%")
            with col4:
                st.metric("No Action Rate", f"{query_analysis.get('no_action_rate', 0):.1f}%")

            col1, col2 = st.columns(2)

            with col1:
                st.markdown("**Most Common Query Terms**")
                top_terms = query_analysis.get("top_terms", {})
                if top_terms:
                    term_df = pd.DataFrame([
                        {"Term": term, "Count": count}
                        for term, count in list(top_terms.items())[:15]
                    ])
                    st.dataframe(term_df, use_container_width=True, hide_index=True)

            with col2:
                st.markdown("**Query Refinements** (users re-querying within 15 min)")
                refinements = query_analysis.get("refinements", [])
                if refinements:
                    for ref in refinements[:5]:
                        with st.expander(f"{ref.get('user', 'Unknown')} - {ref.get('time_gap_minutes', 0)} min gap"):
                            st.text(f"Query 1: {ref.get('query1', '')}")
                            st.text(f"Query 2: {ref.get('query2', '')}")
                            st.text(f"Category: {ref.get('category', 'N/A')}")
                else:
                    st.info("No query refinements detected")

            st.markdown("**Queries Without Follow-up Actions** (Potential Unmet Needs)")
            no_action = query_analysis.get("no_action_examples", [])
            if no_action:
                no_action_df = pd.DataFrame(no_action)
                st.dataframe(no_action_df, use_container_width=True, hide_index=True)
            else:
                st.success("All queries have follow-up actions!")
        else:
            st.info("No query data available for analysis")

        # Feature #2: Resource Effectiveness
        st.divider()
        st.subheader("⚡ Resource Effectiveness Scoring")

        with st.spinner("Analyzing resource effectiveness..."):
            resource_analysis = analyzer.analyze_resource_effectiveness()

        if resource_analysis and "message" not in resource_analysis:
            col1, col2, col3, col4 = st.columns(4)
            with col1:
                st.metric("Total Searches", resource_analysis.get("total_referrals", 0))
            with col2:
                st.metric("Action Plans Created", resource_analysis.get("total_action_plans", 0))
            with col3:
                st.metric("Conversion Rate", f"{resource_analysis.get('overall_conversion_rate', 0):.1f}%")
            with col4:
                users_both = resource_analysis.get("users_both", 0)
                users_searched = resource_analysis.get("users_searched", 1)
                st.metric("User Conversion", f"{users_both}/{users_searched}")

            st.markdown("**Category Effectiveness** (Search -> Action Plan Conversion)")
            effectiveness = resource_analysis.get("category_effectiveness", [])
            if effectiveness:
                eff_df = pd.DataFrame(effectiveness)

                import altair as alt
                # Build tooltip from actual columns in eff_df
                tooltip_cols = [c for c in eff_df.columns if c in ("category", "conversion_rate", "search_count", "act_count", "search_type", "act_type") or c in eff_df.columns]
                chart = alt.Chart(eff_df).mark_bar().encode(
                    x=alt.X("conversion_rate:Q", title="Conversion Rate (%)"),
                    y=alt.Y("category:N", sort="-x", title="Category"),
                    color=alt.Color("conversion_rate:Q", scale=alt.Scale(scheme="blues")),
                    tooltip=[alt.Tooltip(c) for c in eff_df.columns],
                ).properties(height=400)

                st.altair_chart(chart, use_container_width=True)
                st.dataframe(eff_df, use_container_width=True, hide_index=True)
        else:
            st.info(resource_analysis.get("message", "No resource effectiveness data available") if resource_analysis else "No resource effectiveness data available")

        # Feature #3: User Journey & Workflow Analytics
        st.divider()
        st.subheader("🛣️ User Journey & Workflow Analytics")

        with st.spinner("Analyzing user journeys..."):
            journey_analysis = analyzer.analyze_user_journeys()

        if journey_analysis:
            col1, col2, col3, col4 = st.columns(4)
            with col1:
                st.metric("Total Sessions", journey_analysis.get("total_sessions", 0))
            with col2:
                st.metric("Avg Session Duration", f"{journey_analysis.get('avg_session_duration', 0):.1f} min")
            with col3:
                st.metric("Multi-Category Rate", f"{journey_analysis.get('multi_category_rate', 0):.1f}%")
            with col4:
                st.metric("Drop-off Rate", f"{journey_analysis.get('drop_off_rate', 0):.1f}%")

            col1, col2 = st.columns(2)

            with col1:
                st.markdown("**Time to Action** (First search -> First action)")
                st.metric("Average", f"{journey_analysis.get('avg_time_to_action', 0):.1f} minutes")
                st.metric("Median", f"{journey_analysis.get('median_time_to_action', 0):.1f} minutes")

                time_examples = journey_analysis.get("time_to_action_examples", [])
                if time_examples:
                    st.markdown("**Fastest Users:**")
                    for ex in time_examples:
                        st.text(f"{ex.get('user', 'Unknown')}: {ex.get('time_minutes', 0)} min")

            with col2:
                st.markdown("**Drop-off Analysis**")
                users_dropped = journey_analysis.get("users_dropped", 0)
                st.metric("Users Who Searched But Never Acted", users_dropped)

                dropped_cats = journey_analysis.get("dropped_user_categories", {})
                if dropped_cats:
                    st.markdown("**Categories where users dropped off:**")
                    for cat, count in list(dropped_cats.items())[:5]:
                        st.text(f"{cat}: {count} users")

            st.markdown("**Sample User Sessions**")
            session_examples = journey_analysis.get("session_examples", [])
            if session_examples:
                for sess in session_examples[:3]:
                    with st.expander(f"{sess.get('user', 'Unknown')} - {sess.get('duration_minutes', 0):.1f} min session"):
                        st.text(f"Traces: {sess.get('trace_count', 0)}")
                        st.text(f"Types: {', '.join(sess.get('types', []))}")
                        st.text(f"Categories: {', '.join(sess.get('categories', []))}")
        else:
            st.info("No user journey data available for analysis")

        # Feature #4: Performance & Quality Metrics
        st.divider()
        st.subheader("🎯 Performance & Quality Metrics")

        with st.spinner("Analyzing performance..."):
            perf_analysis = analyzer.analyze_performance_quality()

        if perf_analysis:
            col1, col2, col3, col4 = st.columns(4)
            with col1:
                st.metric("Overall Error Rate", f"{perf_analysis.get('overall_error_rate', 0):.1f}%")
            with col2:
                benchmarks = perf_analysis.get("benchmarks", {})
                st.metric("Median Response Time", f"{benchmarks.get('p50', 0):.2f}s")
            with col3:
                st.metric("P95 Response Time", f"{benchmarks.get('p95', 0):.2f}s")
            with col4:
                llm_perf = perf_analysis.get("llm_performance", {})
                st.metric("Total LLM Calls", llm_perf.get("total_llm_calls", 0))

            col1, col2 = st.columns(2)

            with col1:
                st.markdown("**Response Time Benchmarks**")
                if benchmarks:
                    bench_df = pd.DataFrame([
                        {"Percentile": "P50 (Median)", "Time (s)": f"{benchmarks.get('p50', 0):.2f}"},
                        {"Percentile": "P75", "Time (s)": f"{benchmarks.get('p75', 0):.2f}"},
                        {"Percentile": "P90", "Time (s)": f"{benchmarks.get('p90', 0):.2f}"},
                        {"Percentile": "P95", "Time (s)": f"{benchmarks.get('p95', 0):.2f}"},
                        {"Percentile": "P99", "Time (s)": f"{benchmarks.get('p99', 0):.2f}"},
                    ])
                    st.dataframe(bench_df, use_container_width=True, hide_index=True)

                st.markdown("**LLM Performance**")
                if llm_perf:
                    st.text(f"Avg LLM Latency: {llm_perf.get('avg_llm_latency_s', 0):.2f}s")
                    st.text(f"Median LLM Latency: {llm_perf.get('median_llm_latency_s', 0):.2f}s")
                    st.text(f"P95 LLM Latency: {llm_perf.get('p95_llm_latency_s', 0):.2f}s")

            with col2:
                st.markdown("**Token Usage**")
                token_usage = perf_analysis.get("token_usage", {})
                col_a, col_b = st.columns(2)
                with col_a:
                    st.metric("Total Tokens", f"{token_usage.get('total_tokens', 0):,}")
                with col_b:
                    st.metric("Avg per Trace", f"{token_usage.get('avg_tokens_per_trace', 0):,.0f}")

                st.markdown("**Error Rates by Category**")
                error_by_cat = perf_analysis.get("error_rate_by_category", {})
                if error_by_cat:
                    error_df = pd.DataFrame([
                        {
                            "Category": cat,
                            "Total": data["total"],
                            "Errors": data["errors"],
                            "Error Rate": f"{data['error_rate']:.1f}%",
                        }
                        for cat, data in error_by_cat.items() if cat and data["total"] > 0
                    ])
                    if not error_df.empty:
                        st.dataframe(error_df.head(10), use_container_width=True, hide_index=True)

            slow_traces = perf_analysis.get("slow_traces", [])
            if slow_traces:
                st.markdown(f"**Slowest Traces** (>{perf_analysis.get('slow_threshold_s', 10):.1f}s)")
                slow_df = pd.DataFrame(slow_traces)
                st.dataframe(slow_df, use_container_width=True, hide_index=True)
        else:
            st.info("No performance data available")

        # Feature #5: Geographic Service Gap Analysis
        st.divider()
        st.subheader("🗺️ Geographic Service Gap Analysis")

        with st.spinner("Analyzing geographic coverage..."):
            geo_analysis = analyzer.analyze_geographic_gaps()

        if geo_analysis:
            col1, col2, col3, col4 = st.columns(4)
            with col1:
                st.metric("Total Zip Mentions", geo_analysis.get("total_zip_mentions", 0))
            with col2:
                st.metric("Unique Zip Codes", geo_analysis.get("unique_zips", 0))
            with col3:
                st.metric("In-Region Zips", geo_analysis.get("in_region_zips", 0))
            with col4:
                st.metric("Out-of-Region", geo_analysis.get("out_of_region_zips", 0))

            col1, col2 = st.columns(2)

            with col1:
                st.markdown("**Primary Region Coverage**")
                coverage = geo_analysis.get("coverage_summary", {})
                if coverage:
                    coverage_df = pd.DataFrame([
                        {"Area": area, "Zip Codes Served": count}
                        for area, count in coverage.items()
                    ])
                    st.dataframe(coverage_df, use_container_width=True, hide_index=True)

                st.markdown("**In-Region Details**")
                in_region = geo_analysis.get("in_region_details", {})
                if in_region:
                    region_df = pd.DataFrame([
                        {"Zip": z, "City": data["city"], "Requests": data["count"], "Categories": ", ".join(data["categories"][:2])}
                        for z, data in list(in_region.items())[:10]
                    ])
                    st.dataframe(region_df, use_container_width=True, hide_index=True)

            with col2:
                st.markdown("**Out-of-Region Requests**")
                out_region = geo_analysis.get("out_of_region_details", {})
                if out_region:
                    out_df = pd.DataFrame([
                        {"Zip": z, "City": data["city"], "Requests": data["count"], "Categories": ", ".join(data["categories"][:2])}
                        for z, data in list(out_region.items())[:10]
                    ])
                    st.dataframe(out_df, use_container_width=True, hide_index=True)
                else:
                    st.success("All requests are within the configured service region!")

                st.markdown("**Locations Without Zip Codes** (Potential Service Gaps)")
                no_zip = geo_analysis.get("location_without_zip", {})
                if no_zip:
                    for loc, count in list(no_zip.items())[:5]:
                        st.text(f"{loc}: {count} mentions")
                else:
                    st.success("All location requests have associated zip codes")

            cat_by_region = geo_analysis.get("category_by_region", {})
            if cat_by_region:
                st.markdown("**Category Demand by Region**")
                region_names = list(cat_by_region.keys())
                region_cols = st.columns(min(len(region_names), 3))
                for i, region_name in enumerate(region_names):
                    with region_cols[i % len(region_cols)]:
                        st.markdown(f"*{region_name}*")
                        region_data = cat_by_region[region_name]
                        if region_data:
                            for cat, count in sorted(region_data.items(), key=lambda x: x[1], reverse=True)[:5]:
                                st.text(f"{cat}: {count}")
        else:
            st.info("No geographic data available")

        # Feature #6: Comparative Period Analysis
        st.divider()
        st.subheader("📅 Comparative Period Analysis")
        st.markdown("*Compare metrics between two time periods*")

        col1, col2 = st.columns(2)
        with col1:
            st.markdown("**Period 1 (Previous)**")
            _p1_days = st.slider("Days ago (start)", 7, 30, 14, key="p1_days")
        with col2:
            st.markdown("**Period 2 (Recent)**")
            p2_days = st.slider("Days to compare", 3, 14, 7, key="p2_days")

        now = datetime.now(timezone.utc)
        p2_end = now
        p2_start = now - pd.Timedelta(days=p2_days)
        p1_end = p2_start
        p1_start = p1_end - pd.Timedelta(days=p2_days)

        with st.spinner("Comparing periods..."):
            compare_analysis = analyzer.analyze_comparative_periods(p1_start, p1_end, p2_start, p2_end)

        if compare_analysis:
            p1 = compare_analysis.get("period1", {})
            p2 = compare_analysis.get("period2", {})
            changes = compare_analysis.get("changes", {})

            st.markdown(
                f"**Comparing:** {p1.get('start', '')[:10]} to {p1.get('end', '')[:10]} "
                f"vs {p2.get('start', '')[:10]} to {p2.get('end', '')[:10]}"
            )

            trajectory = compare_analysis.get("growth_trajectory", "stable")
            trajectory_icon = "📈" if trajectory == "growing" else ("📉" if trajectory == "declining" else "➡️")
            st.markdown(f"**Overall Trajectory:** {trajectory_icon} {trajectory.upper()}")

            p2_metrics = p2.get("metrics", {})

            # Show dynamic metrics
            metric_pairs = [
                ("Traces", "total_traces", "traces_change"),
                ("Users", "unique_users", "users_change"),
            ]
            # Add dynamic type changes
            for ttype in detected_trace_types:
                metric_pairs.append((
                    ttype.replace("_", " ").title(),
                    ttype,
                    f"{ttype}_change",
                ))
            metric_pairs.append(("Avg Duration", "avg_duration_s", "duration_change"))

            compare_cols = st.columns(min(len(metric_pairs), 6))
            for i, (label, metric_key, change_key) in enumerate(metric_pairs):
                with compare_cols[i % len(compare_cols)]:
                    val = p2_metrics.get(metric_key, 0)
                    delta = changes.get(change_key, 0)
                    display_val = f"{val:.1f}s" if "duration" in metric_key else str(val)
                    st.metric(label, display_val, f"{delta:+.1f}%" if delta != 0 else "0%")

            cat_changes = compare_analysis.get("category_changes", {})
            if cat_changes:
                st.markdown("**Category Shifts**")
                cat_df = pd.DataFrame([
                    {
                        "Category": cat,
                        "Period 1": data["period1"],
                        "Period 2": data["period2"],
                        "Change": f"{data['change_pct']:+.0f}%",
                    }
                    for cat, data in cat_changes.items()
                    if cat and (data["period1"] > 0 or data["period2"] > 0)
                ])
                if not cat_df.empty:
                    st.dataframe(
                        cat_df.sort_values("Period 2", ascending=False).head(10),
                        use_container_width=True,
                        hide_index=True,
                    )

            col1, col2 = st.columns(2)
            with col1:
                new_cats = compare_analysis.get("new_categories", [])
                if new_cats:
                    st.markdown("**New Categories in Period 2:**")
                    for cat in new_cats:
                        st.text(f"  {cat}")
            with col2:
                dropped_cats = compare_analysis.get("dropped_categories", [])
                if dropped_cats:
                    st.markdown("**Categories Not in Period 2:**")
                    for cat in dropped_cats:
                        st.text(f"  {cat}")
        else:
            st.info("Not enough data for period comparison")

        # Feature #7: System Health & Alerts
        st.divider()
        st.subheader("🚨 System Health & Alerts")

        with st.spinner("Checking system health..."):
            alert_analysis = analyzer.analyze_realtime_alerts()

        if alert_analysis:
            status = alert_analysis.get("status", "healthy")
            status_colors = {
                "healthy": "🟢",
                "attention": "🟡",
                "warning": "🟠",
                "critical": "🔴",
            }
            st.markdown(f"### System Status: {status_colors.get(status, '⚪')} {status.upper()}")

            health_checks = alert_analysis.get("health_checks", {})
            if health_checks:
                hc_cols = st.columns(min(len(health_checks), 5))
                check_icons = {"ok": "✅", "issue": "⚠️"}
                for i, (check, result) in enumerate(health_checks.items()):
                    with hc_cols[i % len(hc_cols)]:
                        st.markdown(f"{check_icons.get(result, '?')} **{check.replace('_', ' ').title()}**")

            alert_counts = alert_analysis.get("alert_counts", {})
            col1, col2, col3 = st.columns(3)
            with col1:
                st.metric("Critical Alerts", alert_counts.get("critical", 0))
            with col2:
                st.metric("Warnings", alert_counts.get("warning", 0))
            with col3:
                st.metric("Info Notices", alert_counts.get("info", 0))

            alerts = alert_analysis.get("alerts", [])
            if alerts:
                st.markdown("**Active Alerts**")
                for alert in alerts:
                    severity = alert.get("severity", "info")
                    icon = {"critical": "🔴", "warning": "🟠", "info": "🔵"}[severity]
                    with st.expander(f"{icon} [{severity.upper()}] {alert.get('type', 'Unknown').replace('_', ' ').title()}"):
                        st.markdown(f"**Message:** {alert.get('message', '')}")
                        st.markdown(f"**Value:** {alert.get('value', 'N/A')}")
                        st.markdown(f"**Threshold:** {alert.get('threshold', 'N/A')}")
            else:
                st.success("No alerts! All systems operating normally.")
        else:
            st.info("Unable to perform health check")

        # Feature #8: AI-Powered Query Understanding
        st.divider()
        st.subheader("🤖 AI-Powered Query Understanding")
        st.markdown("*Deep analysis of query patterns, intents, entities, and quality*")

        with st.spinner("Analyzing queries with AI..."):
            query_intel = analyzer.analyze_query_intelligence()

        if query_intel:
            col1, col2, col3, col4 = st.columns(4)
            with col1:
                st.metric("Queries Analyzed", query_intel.get("total_queries_analyzed", 0))
            with col2:
                quality_stats = query_intel.get("quality_stats", {})
                st.metric("Avg Quality Score", f"{quality_stats.get('avg', 0):.0f}/100")
            with col3:
                complexity_stats = query_intel.get("complexity_stats", {})
                st.metric("Avg Complexity", f"{complexity_stats.get('avg', 0):.1f}/10")
            with col4:
                st.metric("Need Improvement", f"{quality_stats.get('needs_improvement_pct', 0):.0f}%")

            insights = query_intel.get("insights", [])
            if insights:
                st.markdown("### AI-Generated Insights")
                for insight in insights:
                    st.info(insight)

            col1, col2 = st.columns(2)

            with col1:
                st.markdown("**Intent Classification**")
                intent_dist = query_intel.get("intent_distribution", {})
                if intent_dist:
                    intent_df = pd.DataFrame([
                        {"Intent": k.replace("_", " ").title(), "Count": v}
                        for k, v in sorted(intent_dist.items(), key=lambda x: x[1], reverse=True)
                    ])
                    st.dataframe(intent_df, use_container_width=True, hide_index=True)

                st.markdown("**Urgency Levels**")
                urgency_dist = query_intel.get("urgency_distribution", {})
                if urgency_dist:
                    urgency_icons = {"critical": "🔴", "high": "🟠", "moderate": "🟡", "normal": "🟢"}
                    for level in ["critical", "high", "moderate", "normal"]:
                        count = urgency_dist.get(level, 0)
                        if count > 0:
                            st.text(f"{urgency_icons.get(level, '⚪')} {level.title()}: {count}")

            with col2:
                st.markdown("**Extracted Entities**")
                entity_summary = query_intel.get("entity_summary", {})

                with st.expander("Client Demographics"):
                    demos = entity_summary.get("top_demographics", {})
                    if demos:
                        for demo, count in demos.items():
                            st.text(f"{demo.replace('_', ' ').title()}: {count}")
                    else:
                        st.text("No demographics detected")

                with st.expander("Services Requested"):
                    services = entity_summary.get("top_services", {})
                    if services:
                        for service, count in services.items():
                            st.text(f"{service.replace('_', ' ').title()}: {count}")
                    else:
                        st.text("No services detected")

                with st.expander("Locations Mentioned"):
                    locations = entity_summary.get("top_locations", {})
                    if locations:
                        for loc, count in locations.items():
                            st.text(f"{loc}: {count}")
                    else:
                        st.text("No locations detected")

            st.markdown("**Common Query Patterns**")
            patterns = query_intel.get("common_patterns", {})
            if patterns:
                pattern_df = pd.DataFrame([
                    {"Pattern": k, "Frequency": len(v), "Example": v[0][:60] + "..." if len(v[0]) > 60 else v[0]}
                    for k, v in list(patterns.items())[:8]
                ])
                st.dataframe(pattern_df, use_container_width=True, hide_index=True)

            # Quality Analysis
            col1, col2 = st.columns(2)
            with col1:
                st.markdown("**High-Quality Query Examples**")
                high_quality = query_intel.get("sample_high_quality", [])
                if high_quality:
                    for q in high_quality[:3]:
                        with st.expander(f"Score: {q['quality_score']}/100 - {q['query'][:40]}..."):
                            st.text(f"Query: {q['query']}")
                            st.text(f"Services: {', '.join(q['services']) if q['services'] else 'None'}")
                            st.text(f"Demographics: {', '.join(q['demographics']) if q['demographics'] else 'None'}")
                            st.text(f"Locations: {', '.join(q['locations']) if q['locations'] else 'None'}")
                            st.text(f"Complexity: {q['complexity']}/10")
                else:
                    st.info("No high-quality queries found")
            with col2:
                st.markdown("**Queries Needing Improvement**")
                needs_improvement = query_intel.get("sample_needs_improvement", [])
                if needs_improvement:
                    for q in needs_improvement[:3]:
                        with st.expander(f"Score: {q['quality_score']}/100 - {q['query'][:40]}..."):
                            st.text(f"Query: {q['query']}")
                            if q["suggestions"]:
                                st.markdown("**Suggestions:**")
                                for sug in q["suggestions"]:
                                    st.text(f"  {sug}")
                else:
                    st.success("All queries meet quality standards!")

            # Detail browser
            st.markdown("**Query Detail Browser**")
            all_analyzed = query_intel.get("all_analyzed", [])
            if all_analyzed:
                col1, col2, col3 = st.columns(3)
                with col1:
                    intent_filter = st.selectbox(
                        "Filter by Intent",
                        ["All"] + list(set(q["intent"] for q in all_analyzed)),
                        key="intent_filter",
                    )
                with col2:
                    urgency_filter = st.selectbox(
                        "Filter by Urgency",
                        ["All", "critical", "high", "moderate", "normal"],
                        key="urgency_filter",
                    )
                with col3:
                    quality_filter = st.selectbox(
                        "Filter by Quality",
                        ["All", "High (70+)", "Medium (50-69)", "Low (<50)"],
                        key="quality_filter",
                    )

                filtered = all_analyzed
                if intent_filter != "All":
                    filtered = [q for q in filtered if q["intent"] == intent_filter]
                if urgency_filter != "All":
                    filtered = [q for q in filtered if q["urgency"] == urgency_filter]
                if quality_filter == "High (70+)":
                    filtered = [q for q in filtered if q["quality_score"] >= 70]
                elif quality_filter == "Medium (50-69)":
                    filtered = [q for q in filtered if 50 <= q["quality_score"] < 70]
                elif quality_filter == "Low (<50)":
                    filtered = [q for q in filtered if q["quality_score"] < 50]

                st.text(f"Showing {len(filtered)} queries")

                if filtered:
                    query_display = pd.DataFrame([
                        {
                            "Query": q["query"][:50] + "..." if len(q["query"]) > 50 else q["query"],
                            "Intent": q["intent"].replace("_", " ").title(),
                            "Urgency": q["urgency"].title(),
                            "Quality": q["quality_score"],
                            "Complexity": q["complexity"],
                            "Services": ", ".join(q["services"][:2]) if q["services"] else "-",
                            "Demographics": ", ".join(q["demographics"][:2]) if q["demographics"] else "-",
                        }
                        for q in filtered[:20]
                    ])
                    st.dataframe(query_display, use_container_width=True, hide_index=True)
        else:
            st.info("No query data available for AI analysis")

        # Output Quality Analysis
        st.divider()
        st.subheader("📊 Output Quality Analysis")
        st.markdown("*Analyzing the quality, relevance, and completeness of AI responses*")

        with st.spinner("Analyzing output quality..."):
            output_quality = analyzer.analyze_output_quality()

        if output_quality:
            quality_stats = output_quality.get("quality_stats", {})
            alignment_stats = output_quality.get("alignment_stats", {})

            col1, col2, col3, col4 = st.columns(4)
            with col1:
                st.metric("Responses Analyzed", output_quality.get("total_analyzed", 0))
            with col2:
                avg_score = quality_stats.get("avg_score", 0)
                st.metric(
                    "Avg Quality Score", f"{avg_score:.0f}/100",
                    delta="Good" if avg_score >= 70 else ("Fair" if avg_score >= 50 else "Needs Work"),
                )
            with col3:
                st.metric("Location Match Rate", f"{alignment_stats.get('location_match_rate', 0):.0f}%")
            with col4:
                st.metric("Category Match Rate", f"{alignment_stats.get('category_match_rate', 0):.0f}%")

            col1, col2, col3, col4 = st.columns(4)
            with col1:
                st.metric("Has Resources", f"{alignment_stats.get('has_resources_rate', 0):.0f}%")
            with col2:
                st.metric("Has Contact Info", f"{alignment_stats.get('actionable_rate', 0):.0f}%")
            with col3:
                st.metric("High Quality", quality_stats.get("high_quality_count", 0))
            with col4:
                st.metric("Low Quality", quality_stats.get("low_quality_count", 0))

            insights = output_quality.get("insights", [])
            if insights:
                st.markdown("### Output Quality Insights")
                for insight in insights:
                    if any(insight.startswith(p) for p in ("⚠️", "📍", "🏷️", "📋", "🔄")):
                        st.warning(insight)
                    elif insight.startswith("✅"):
                        st.success(insight)
                    else:
                        st.info(insight)

            # Issue breakdown
            st.markdown("### Issue Detection")
            issue_counts = output_quality.get("issue_counts", {})

            col1, col2, col3 = st.columns(3)
            with col1:
                st.metric("No Resources", issue_counts.get("no_resources", 0),
                          delta="issue" if issue_counts.get("no_resources", 0) > 5 else None, delta_color="inverse")
                st.metric("Generic Responses", issue_counts.get("generic_response", 0),
                          delta="issue" if issue_counts.get("generic_response", 0) > 3 else None, delta_color="inverse")
            with col2:
                st.metric("Location Mismatch", issue_counts.get("location_mismatch", 0),
                          delta="issue" if issue_counts.get("location_mismatch", 0) > 5 else None, delta_color="inverse")
                st.metric("Category Mismatch", issue_counts.get("category_mismatch", 0),
                          delta="issue" if issue_counts.get("category_mismatch", 0) > 5 else None, delta_color="inverse")
            with col3:
                st.metric("Low Quality", issue_counts.get("low_quality", 0),
                          delta="issue" if issue_counts.get("low_quality", 0) > 5 else None, delta_color="inverse")
                st.metric("Too Short", issue_counts.get("too_short", 0),
                          delta="issue" if issue_counts.get("too_short", 0) > 3 else None, delta_color="inverse")

            issues = output_quality.get("issues", {})
            if any(issues.values()):
                st.markdown("### Issue Examples")
                issue_tab_names = ["Location Mismatch", "Category Mismatch", "No Resources", "Generic", "Low Quality"]
                issue_keys = ["location_mismatch", "category_mismatch", "no_resources", "generic_response", "low_quality"]
                issue_tabs = st.tabs(issue_tab_names)

                for tab_idx, (issue_tab, issue_key) in enumerate(zip(issue_tabs, issue_keys)):
                    with issue_tab:
                        examples = issues.get(issue_key, [])
                        if examples:
                            for item in examples[:3]:
                                with st.expander(f"Score: {item['quality_score']} - {item['query'][:50]}..."):
                                    st.text(f"User: {item['user']}")
                                    st.text(f"Query: {item['query']}")
                                    if item.get("factors"):
                                        st.text(f"Factors: {', '.join(item['factors'])}")
                        else:
                            st.success(f"No {issue_tab_names[tab_idx].lower()} issues!")

            # Best vs Worst responses
            col1, col2 = st.columns(2)
            with col1:
                st.markdown("**Best Quality Responses**")
                best = output_quality.get("best_responses", [])
                if best:
                    for item in best[:5]:
                        with st.expander(f"Score: {item['quality_score']}/100"):
                            st.text(f"Query: {item['query']}")
                            st.text(f"User: {item['user']}")
                            st.text(f"Resources: {item['resource_count']}")
                            st.text(f"Has Contact Info: {'Yes' if item['has_actionable_info'] else 'No'}")
                            st.text(f"Positive Factors: {', '.join([f for f in item.get('factors', []) if not f.startswith('-')])}")
            with col2:
                st.markdown("**Responses Needing Review**")
                worst = output_quality.get("all_analyses", [])
                if worst:
                    for item in worst[:5]:
                        with st.expander(f"Score: {item['quality_score']}/100"):
                            st.text(f"Query: {item['query']}")
                            st.text(f"User: {item['user']}")
                            st.text(f"Issues: {', '.join(item.get('factors', []))}")
        else:
            st.info("No output data available for quality analysis")

        # Resource Recommendation Analysis
        st.divider()
        st.subheader("📚 Resource Recommendation Analysis")
        st.markdown("*Tracking which resources are recommended and their effectiveness*")

        with st.spinner("Analyzing resource recommendations..."):
            resource_rec_analysis = analyzer.analyze_resource_recommendations()

        if resource_rec_analysis:
            if resource_rec_analysis.get("note"):
                st.warning(resource_rec_analysis["note"])

            col1, col2, col3, col4 = st.columns(4)
            with col1:
                st.metric("Total Recommendations", resource_rec_analysis.get("total_recommendations", 0))
            with col2:
                st.metric("Unique Resources", resource_rec_analysis.get("unique_resources", 0))
            with col3:
                st.metric("Avg per Trace", f"{resource_rec_analysis.get('recommendations_per_trace', 0):.1f}")
            with col4:
                concentration = resource_rec_analysis.get("concentration_ratio", 0)
                st.metric(
                    "Top 5 Concentration", f"{concentration:.0f}%",
                    delta="High" if concentration > 50 else ("Balanced" if concentration > 30 else "Diverse"),
                )

            col1, col2, col3, col4 = st.columns(4)
            with col1:
                st.metric("Diversity Score", f"{resource_rec_analysis.get('diversity_score', 0):.0f}/100")
            with col2:
                st.metric("Location Match Rate", f"{resource_rec_analysis.get('location_match_rate', 0):.0f}%")
            with col3:
                st.metric("Traces with Resources", resource_rec_analysis.get("traces_with_resources", 0))
            with col4:
                st.metric("Traces without Resources", resource_rec_analysis.get("traces_without_resources", 0))

            insights = resource_rec_analysis.get("insights", [])
            if insights:
                st.markdown("### Resource Insights")
                for insight in insights:
                    if insight.startswith("⚠️"):
                        st.warning(insight)
                    elif insight.startswith("✅"):
                        st.success(insight)
                    else:
                        st.info(insight)

            col1, col2 = st.columns(2)
            with col1:
                st.markdown("**Most Recommended Resources**")
                top_resources = resource_rec_analysis.get("top_resources", [])
                if top_resources:
                    resource_df = pd.DataFrame([
                        {
                            "Resource": r["name"][:40] + "..." if len(r["name"]) > 40 else r["name"],
                            "Count": r["count"],
                            "%": f"{r['percentage']:.1f}%",
                        }
                        for r in top_resources[:10]
                    ])
                    st.dataframe(resource_df, use_container_width=True, hide_index=True)
                else:
                    st.info("No resource data available")

                st.markdown("**Resource Categories**")
                cat_dist = resource_rec_analysis.get("category_distribution", {})
                if cat_dist:
                    cat_df = pd.DataFrame([
                        {"Category": k, "Count": v}
                        for k, v in list(cat_dist.items())[:10]
                    ])
                    st.dataframe(cat_df, use_container_width=True, hide_index=True)

            with col2:
                st.markdown("**Geographic Distribution**")
                geo_dist = resource_rec_analysis.get("geographic_distribution", {})
                if geo_dist:
                    by_city = geo_dist.get("by_city", {})
                    if by_city:
                        st.markdown("**By City:**")
                        city_df = pd.DataFrame([
                            {"City": k, "Count": v}
                            for k, v in list(by_city.items())[:8]
                        ])
                        st.dataframe(city_df, use_container_width=True, hide_index=True)

                    by_zip = geo_dist.get("by_zip", {})
                    if by_zip:
                        with st.expander("By Zip Code"):
                            zip_df = pd.DataFrame([
                                {"Zip": k, "Count": v}
                                for k, v in list(by_zip.items())[:10]
                            ])
                            st.dataframe(zip_df, use_container_width=True, hide_index=True)

                    out_of_region = geo_dist.get("out_of_region_zips", [])
                    if out_of_region:
                        st.warning(f"Out-of-region zips: {', '.join(out_of_region)}")

            st.markdown("**Resource Detail Browser**")
            sample_resources = resource_rec_analysis.get("sample_resources", [])
            if sample_resources:
                res_display = pd.DataFrame([
                    {
                        "Name": r.get("name", "Unknown")[:30],
                        "Category": r.get("category", "-"),
                        "City": r.get("city", "-"),
                        "Zip": r.get("zip_code", "-"),
                        "Phone": "Y" if r.get("phone") else "-",
                        "Website": "Y" if r.get("website") else "-",
                    }
                    for r in sample_resources[:15]
                ])
                st.dataframe(res_display, use_container_width=True, hide_index=True)
        else:
            st.info("No resource recommendation data available")

    # ==================================================================
    # TAB 7: Evals
    # ==================================================================
    with tab7:
        st.header("Comprehensive AI Output Evaluation")
        st.markdown(
            "*Automated quality checks, hallucination detection, and failure taxonomy for confident leadership reporting.*"
        )

        eval_tabs = st.tabs([
            "📊 Executive Summary",
            "🚨 Failure Taxonomy",
            "🔍 Resource Validation",
            "📈 Coverage Analysis",
            "🧪 Test Cases",
            "🔄 Consistency",
            "📋 Detailed Flags",
        ])

        # Resource inventory configuration
        with st.sidebar.expander("📦 Resource Inventory (S3)", expanded=False):
            st.markdown("**Connect to your S3 resource inventory for hallucination detection.**")
            s3_bucket = st.text_input("S3 Bucket Name", value=os.getenv("RESOURCE_S3_BUCKET", ""))
            s3_key = st.text_input("S3 Object Key", value=os.getenv("RESOURCE_S3_KEY", "resources.json"))
            aws_region = st.text_input("AWS Region", value=os.getenv("AWS_REGION", "us-east-1"))
            use_inventory = st.checkbox("Enable inventory validation", value=False)

            st.markdown("**Or upload inventory CSV/JSON:**")
            inventory_file = st.file_uploader("Upload resource inventory", type=["csv", "json"], key="inventory_upload")

        resource_inventory = []
        if inventory_file is not None:
            try:
                if inventory_file.name.endswith(".json"):
                    import json as json_lib
                    resource_inventory = json_lib.load(inventory_file)
                    if isinstance(resource_inventory, dict):
                        for key in ["resources", "data", "items"]:
                            if key in resource_inventory:
                                resource_inventory = resource_inventory[key]
                                break
                else:
                    inv_df = pd.read_csv(inventory_file)
                    resource_inventory = inv_df.to_dict("records")
                st.sidebar.success(f"Loaded {len(resource_inventory)} resources from file")
            except Exception as e:
                st.sidebar.error(f"Error loading inventory: {e}")
        elif use_inventory and s3_bucket:
            try:
                import boto3
                s3_client = boto3.client("s3", region_name=aws_region)
                response = s3_client.get_object(Bucket=s3_bucket, Key=s3_key)
                import json as json_lib
                resource_inventory = json_lib.loads(response["Body"].read().decode("utf-8"))
                if isinstance(resource_inventory, dict):
                    for key in ["resources", "data", "items"]:
                        if key in resource_inventory:
                            resource_inventory = resource_inventory[key]
                            break
                st.sidebar.success(f"Loaded {len(resource_inventory)} resources from S3")
            except Exception as e:
                st.sidebar.warning(f"Could not load from S3: {e}")

        # Test case upload
        with st.sidebar.expander("🧪 Gold Standard Test Cases", expanded=False):
            st.markdown("Upload test cases (JSON format):")
            st.markdown("""
            ```json
            [
              {
                "query": "example query",
                "expected_resources": ["Resource A"],
                "expected_category": "category",
                "expected_location": "location"
              }
            ]
            ```
            """)
            test_case_file = st.file_uploader("Upload test cases", type=["json"], key="testcase_upload")

        test_cases = []
        if test_case_file is not None:
            try:
                import json as json_lib
                test_cases = json_lib.load(test_case_file)
                st.sidebar.success(f"Loaded {len(test_cases)} test cases")
            except Exception as e:
                st.sidebar.error(f"Error loading test cases: {e}")

        # Run comprehensive evals
        comprehensive_results = analyzer.get_comprehensive_evals(
            resource_inventory=resource_inventory if resource_inventory else None,
            test_cases=test_cases if test_cases else None,
        )

        if comprehensive_results.get("error"):
            st.warning(comprehensive_results["error"])
        else:
            # EVAL TAB: EXECUTIVE SUMMARY
            with eval_tabs[0]:
                st.subheader("📊 Executive Summary for Leadership")
                st.markdown("*One-page view of AI output quality health*")

                exec_summary = comprehensive_results.get("executive_summary", {})
                if exec_summary.get("error"):
                    st.warning(exec_summary["error"])
                else:
                    health_score = exec_summary.get("health_score", 0)
                    health_status = exec_summary.get("health_status", "unknown")
                    status_colors = {
                        "excellent": "🟢", "good": "🟡",
                        "needs_attention": "🟠", "critical": "🔴",
                    }
                    status_emoji = status_colors.get(health_status, "⚪")

                    st.markdown(f"""
                    <div style="background: linear-gradient(135deg, #1e3a5f 0%, #2d5a87 100%);
                                padding: 30px; border-radius: 15px; text-align: center; margin-bottom: 20px;">
                        <h1 style="color: white; margin: 0; font-size: 3rem;">{status_emoji} {health_score}/100</h1>
                        <p style="color: #ccc; margin: 10px 0 0 0; font-size: 1.2rem;">
                            Overall Quality Health Score ({health_status.replace('_', ' ').title()})
                        </p>
                    </div>
                    """, unsafe_allow_html=True)

                    col1, col2, col3, col4 = st.columns(4)
                    col1.metric("Pass Rate", f"{exec_summary.get('pass_rate', 0):.1f}%", help="Traces with no critical failures")
                    col2.metric("Avg Quality Score", f"{exec_summary.get('avg_quality_score', 0):.1f}")
                    col3.metric("Inventory Match", f"{exec_summary.get('match_rate', 0):.1f}%", help="Resources found in known inventory")
                    col4.metric("Actionable Rate", f"{exec_summary.get('actionable_rate', 0):.1f}%", help="Responses with contact details")

                    col1, col2, col3, col4 = st.columns(4)
                    col1.metric("Traces Evaluated", f"{exec_summary.get('total_traces_evaluated', 0):,}")
                    col2.metric("Traces with Failures", f"{exec_summary.get('traces_with_any_failure', 0):,}")
                    col3.metric("Failure Rate", f"{exec_summary.get('failure_rate', 0):.1f}%")
                    col4.metric("Hallucination Rate", f"{exec_summary.get('hallucination_rate', 0):.1f}%",
                                delta=None if exec_summary.get("hallucination_rate", 0) == 0 else "needs inventory")

                    st.markdown("### Top Failure Types")
                    top_failures = exec_summary.get("top_failure_types", [])
                    if top_failures:
                        failure_df = pd.DataFrame([
                            {"Failure Type": f[0].replace("_", " ").title(), "Count": f[1]}
                            for f in top_failures
                        ])
                        fig_failures = px.bar(
                            failure_df, x="Failure Type", y="Count",
                            title="Failure counts by type", color="Count",
                            color_continuous_scale="Reds",
                        )
                        st.plotly_chart(fig_failures, use_container_width=True)
                    else:
                        st.success("No failures detected!")

                    st.markdown("### Recommendations")
                    for rec in exec_summary.get("recommendations", []):
                        if "🚨" in rec or "⚠️" in rec:
                            st.warning(rec)
                        elif "✅" in rec:
                            st.success(rec)
                        else:
                            st.info(rec)

            # EVAL TAB: FAILURE TAXONOMY
            with eval_tabs[1]:
                st.subheader("🚨 Structured Failure Taxonomy")
                st.markdown("*Specific, actionable failure categories for diagnosis*")

                taxonomy = comprehensive_results.get("failure_taxonomy", {})
                failure_counts = taxonomy.get("failure_counts", {})

                col1, col2, col3 = st.columns(3)
                col1.metric("Traces with Failures", taxonomy.get("traces_with_failures", 0))
                col2.metric("Traces without Failures", taxonomy.get("traces_without_failures", 0))
                col3.metric("Failure Rate", f"{taxonomy.get('failure_rate', 0):.1f}%")

                if failure_counts:
                    failure_data = pd.DataFrame([
                        {"Failure Type": k.replace("_", " ").title(), "Count": v}
                        for k, v in failure_counts.items() if v > 0
                    ]).sort_values("Count", ascending=True)

                    if not failure_data.empty:
                        fig = px.bar(
                            failure_data, y="Failure Type", x="Count",
                            orientation="h", title="Failures by Type",
                            color="Count", color_continuous_scale="Reds",
                        )
                        fig.update_layout(height=400)
                        st.plotly_chart(fig, use_container_width=True)

                st.markdown("### Drill-down by Failure Type")
                failure_types = [k for k, v in failure_counts.items() if v > 0]
                if failure_types:
                    selected_failure = st.selectbox("Select failure type to inspect:", failure_types)
                    failures_by_type = taxonomy.get("failures_by_type", {})
                    examples = failures_by_type.get(selected_failure, [])

                    if examples:
                        st.markdown(f"**{len(examples)} examples of {selected_failure.replace('_', ' ')} failures:**")
                        examples_df = pd.DataFrame(examples)
                        st.dataframe(examples_df, use_container_width=True, hide_index=True)
                        csv = examples_df.to_csv(index=False)
                        st.download_button(
                            f"Download {selected_failure} failures",
                            csv, file_name=f"{selected_failure}_failures.csv",
                            mime="text/csv",
                        )
                    else:
                        st.info("No examples available for this failure type.")
                else:
                    st.success("No failures detected in current data!")

            # EVAL TAB: RESOURCE VALIDATION
            with eval_tabs[2]:
                st.subheader("🔍 Resource Validation (Hallucination Detection)")
                st.markdown("*Compare recommended resources against known inventory*")

                validation = comprehensive_results.get("resource_validation", {})
                if validation.get("note"):
                    st.warning(validation["note"])
                    st.info("Upload a resource inventory file in the sidebar to enable validation.")
                else:
                    col1, col2, col3, col4 = st.columns(4)
                    col1.metric("Total Recommended", validation.get("total_resources_recommended", 0))
                    col2.metric("Matched to Inventory", validation.get("matched_to_inventory", 0))
                    col3.metric("Unmatched (Potential Hallucinations)", validation.get("unmatched_count", 0))
                    col4.metric("Inventory Size", validation.get("inventory_size", 0))

                    match_rate = validation.get("match_rate", 0)
                    hallucination_rate = validation.get("hallucination_rate", 0)
                    health = validation.get("health", "unknown")
                    health_colors = {"good": "🟢", "warning": "🟡", "critical": "🔴"}
                    st.markdown(f"""
                    ### {health_colors.get(health, '⚪')} Match Rate: {match_rate:.1f}%
                    **Hallucination Rate: {hallucination_rate:.1f}%**
                    """)

                    if hallucination_rate > 10:
                        st.error(f"High hallucination rate ({hallucination_rate:.1f}%) - review unmatched resources")

                    unmatched = validation.get("unmatched_resources", [])
                    if unmatched:
                        st.markdown("### Unmatched Resources (Potential Hallucinations)")
                        unmatched_df = pd.DataFrame(unmatched)
                        st.dataframe(unmatched_df, use_container_width=True, hide_index=True)
                        csv = unmatched_df.to_csv(index=False)
                        st.download_button("Download unmatched resources", csv, file_name="unmatched_resources.csv", mime="text/csv")

            # EVAL TAB: COVERAGE ANALYSIS
            with eval_tabs[3]:
                st.subheader("📈 Coverage Analysis")
                st.markdown("*Are we recommending the right breadth of resources?*")

                coverage = comprehensive_results.get("coverage_analysis", {})
                if coverage.get("note"):
                    st.warning(coverage["note"])
                    st.info("Upload a resource inventory to enable coverage analysis.")
                else:
                    col1, col2, col3 = st.columns(3)
                    col1.metric("Total in Inventory", coverage.get("total_in_inventory", 0))
                    col2.metric("Ever Recommended", coverage.get("total_ever_recommended", 0))
                    col3.metric("Overall Coverage", f"{coverage.get('overall_coverage_pct', 0):.1f}%")

                    under_utilized = coverage.get("under_utilized_resources", [])
                    st.metric("Under-utilized Resources", coverage.get("under_utilized_count", 0))
                    if under_utilized:
                        st.markdown("### Under-utilized Resources (Never Recommended)")
                        st.write(", ".join(under_utilized[:30]))
                        if len(under_utilized) > 30:
                            st.caption(f"... and {len(under_utilized) - 30} more")

                    cat_coverage = coverage.get("category_coverage", {})
                    if cat_coverage:
                        st.markdown("### Coverage by Category")
                        cat_data = pd.DataFrame([
                            {
                                "Category": k.title(),
                                "In Inventory": v["inventory_count"],
                                "Ever Recommended": v["recommended_count"],
                                "Coverage %": v["coverage_pct"],
                            }
                            for k, v in cat_coverage.items()
                        ]).sort_values("Coverage %")

                        fig = px.bar(
                            cat_data, x="Category", y="Coverage %",
                            title="Coverage % by Category",
                            color="Coverage %", color_continuous_scale="RdYlGn",
                        )
                        st.plotly_chart(fig, use_container_width=True)
                        st.dataframe(cat_data, use_container_width=True, hide_index=True)

            # EVAL TAB: TEST CASES
            with eval_tabs[4]:
                st.subheader("🧪 Gold Standard Test Cases")
                st.markdown("*Automated testing against expected outputs*")

                test_results = comprehensive_results.get("test_case_results", {})
                if test_results.get("note"):
                    st.info(test_results["note"])
                    st.markdown("""
                    **To enable test case evaluation:**
                    1. Create a JSON file with test cases
                    2. Upload it in the sidebar under "Gold Standard Test Cases"
                    """)
                else:
                    col1, col2, col3, col4 = st.columns(4)
                    col1.metric("Total Test Cases", test_results.get("total_test_cases", 0))
                    col2.metric("Passed", test_results.get("passed", 0))
                    col3.metric("Failed", test_results.get("failed", 0))
                    col4.metric("Pass Rate", f"{test_results.get('pass_rate', 0):.1f}%")

                    results = test_results.get("results", [])
                    if results:
                        st.markdown("### Test Case Results")
                        for result in results:
                            status = result.get("status", "unknown")
                            icon = "✅" if status == "pass" else ("❌" if status == "fail" else "⚠️")
                            with st.expander(f"{icon} {result.get('query', 'Unknown query')[:60]}..."):
                                st.markdown(f"**Status:** {status.upper()}")
                                st.markdown(f"**Resource Recall:** {result.get('resource_recall', 0):.1f}%")
                                st.markdown(f"**Resource Precision:** {result.get('resource_precision', 0):.1f}%")
                                st.markdown(f"**Category Match:** {'✅' if result.get('category_match') else '❌'}")
                                st.markdown(f"**Location Match:** {'✅' if result.get('location_match') else '❌'}")
                                if result.get("missing_resources"):
                                    st.warning(f"Missing resources: {', '.join(result['missing_resources'])}")
                                if result.get("extra_resources"):
                                    st.info(f"Extra resources: {', '.join(result['extra_resources'])}")

            # EVAL TAB: CONSISTENCY
            with eval_tabs[5]:
                st.subheader("🔄 Consistency Analysis")
                st.markdown("*Do similar queries produce similar results?*")

                consistency = analyzer.run_consistency_analysis([])

                col1, col2, col3 = st.columns(3)
                col1.metric("Query Groups Analyzed", consistency.get("groups_analyzed", 0))
                col2.metric("Inconsistent Groups", consistency.get("inconsistent_groups", 0))
                col3.metric("Consistency Rate", f"{consistency.get('consistency_rate', 0):.1f}%")

                most_inconsistent = consistency.get("most_inconsistent", [])
                if most_inconsistent:
                    st.markdown("### Most Inconsistent Query Groups")
                    st.caption("These query patterns show high variance in responses")
                    inconsistent_df = pd.DataFrame([
                        {
                            "Query Pattern": r.get("query_group", ""),
                            "Traces": r.get("num_traces", 0),
                            "Quality Variance": f"{r.get('quality_variance', 0):.1f}",
                            "Resource Similarity": f"{r.get('resource_similarity', 0):.1%}",
                        }
                        for r in most_inconsistent
                    ])
                    st.dataframe(inconsistent_df, use_container_width=True, hide_index=True)
                else:
                    st.success("Not enough repeated queries to analyze consistency.")

            # EVAL TAB: DETAILED FLAGS
            with eval_tabs[6]:
                st.subheader("📋 Detailed Automated Flags")
                st.markdown("*Threshold-based flagging with full drill-down*")

                eval_df = analyzer.get_output_quality_evals()
                if eval_df.empty:
                    st.info("No eval data available for automated checks.")
                else:
                    eval_df = eval_df.copy()
                    eval_df["timestamp"] = pd.to_datetime(eval_df.get("timestamp"), errors="coerce")

                    st.markdown("#### Evaluation filters & thresholds")
                    eval_trace_types = sorted([t for t in eval_df["trace_type"].dropna().unique() if t])
                    default_eval_types = [t for t in detected_trace_types if t in eval_trace_types] or eval_trace_types
                    selected_eval_types = st.multiselect(
                        "Trace types to evaluate",
                        eval_trace_types,
                        default=default_eval_types,
                        key="detailed_trace_types",
                    )

                    filter_user_facing = st.checkbox(
                        "Only user-facing traces (non-empty query & real user)",
                        value=True,
                        key="detailed_user_facing",
                    )

                    with st.expander("Eval thresholds", expanded=False):
                        min_quality = st.slider("Minimum quality score", 0, 100, 60, 5, key="detailed_min_quality")
                        min_resources = st.slider("Minimum resources per response", 0, 10, 2, 1, key="detailed_min_resources")
                        min_detail_score = st.slider(
                            "Minimum resource detail score", 0, 100, 50, 5,
                            help="Proxy for hallucination risk: missing location/contact info lowers score.",
                            key="detailed_min_detail",
                        )
                        require_actionable = st.checkbox("Require actionable info when resources are present", value=True, key="detailed_require_actionable")
                        enforce_location = st.checkbox("Enforce location match when user specified a location", value=True, key="detailed_enforce_location")
                        enforce_category = st.checkbox("Enforce category match when category is known", value=False, key="detailed_enforce_category")

                    filtered = eval_df
                    if selected_eval_types:
                        filtered = filtered[filtered["trace_type"].isin(selected_eval_types)]

                    if filter_user_facing:
                        q = filtered["query"].astype(str).str.strip()
                        u = filtered["user"].astype(str).str.lower().str.strip()
                        filtered = filtered[
                            (q != "") & (q != ".") & (~u.str.startswith("unknown")) & (u != "unknown")
                        ]

                    if filtered.empty:
                        st.info("No traces match the current filters.")
                    else:
                        filtered = filtered.copy()
                        filtered["quality_score"] = pd.to_numeric(filtered["quality_score"], errors="coerce").fillna(0)
                        filtered["resource_count"] = pd.to_numeric(filtered["resource_count"], errors="coerce").fillna(0)
                        filtered["resource_detail_score"] = pd.to_numeric(
                            filtered.get("resource_detail_score"), errors="coerce"
                        ).fillna(0)

                        total = len(filtered)
                        avg_quality = filtered["quality_score"].mean()
                        location_match_rate = filtered["location_aligned"].mean() * 100 if "location_aligned" in filtered else 0
                        category_match_rate = filtered["category_aligned"].mean() * 100 if "category_aligned" in filtered else 0
                        resources_rate = (filtered["resource_count"] > 0).mean() * 100
                        avg_detail = filtered["resource_detail_score"].mean()

                        st.markdown("#### Summary")
                        col1, col2, col3, col4, col5, col6 = st.columns(6)
                        col1.metric("Traces", f"{total:,}")
                        col2.metric("Avg Quality", f"{avg_quality:.1f}")
                        col3.metric("Location Match", f"{location_match_rate:.1f}%")
                        col4.metric("Category Match", f"{category_match_rate:.1f}%")
                        col5.metric("Has Resources", f"{resources_rate:.1f}%")
                        col6.metric("Detail Score", f"{avg_detail:.1f}")

                        st.markdown("#### Failure flags")
                        filtered["flag_no_resources"] = filtered["resource_count"] < min_resources
                        filtered["flag_low_quality"] = filtered["quality_score"] < min_quality
                        filtered["flag_low_detail"] = (
                            (filtered["resource_count"] > 0) & (filtered["resource_detail_score"] < min_detail_score)
                        )
                        filtered["flag_generic"] = filtered.get("is_generic", False).fillna(False)
                        filtered["flag_too_short"] = filtered.get("too_short", False).fillna(False)
                        filtered["flag_location_mismatch"] = enforce_location & (~filtered.get("location_aligned", True))
                        filtered["flag_category_mismatch"] = enforce_category & (~filtered.get("category_aligned", True))
                        filtered["flag_not_actionable"] = (
                            require_actionable
                            & (filtered["resource_count"] > 0)
                            & (~filtered.get("has_actionable_info", True))
                        )

                        flag_cols = [
                            "flag_no_resources", "flag_low_quality", "flag_low_detail",
                            "flag_generic", "flag_too_short", "flag_location_mismatch",
                            "flag_category_mismatch", "flag_not_actionable",
                        ]
                        issue_counts_eval = {
                            "No resources": int(filtered["flag_no_resources"].sum()),
                            "Low quality": int(filtered["flag_low_quality"].sum()),
                            "Low detail score": int(filtered["flag_low_detail"].sum()),
                            "Generic response": int(filtered["flag_generic"].sum()),
                            "Too short": int(filtered["flag_too_short"].sum()),
                            "Location mismatch": int(filtered["flag_location_mismatch"].sum()),
                            "Category mismatch": int(filtered["flag_category_mismatch"].sum()),
                            "Not actionable": int(filtered["flag_not_actionable"].sum()),
                        }

                        issues_df = pd.DataFrame(
                            [{"Issue": k, "Count": v} for k, v in issue_counts_eval.items() if v > 0]
                        ).sort_values("Count", ascending=False)

                        if not issues_df.empty:
                            fig_issues = px.bar(issues_df, x="Issue", y="Count", title="Flag counts by issue type")
                            st.plotly_chart(fig_issues, use_container_width=True)
                        else:
                            st.success("No automated failures for the current thresholds.")

                        flagged = filtered[filtered[flag_cols].any(axis=1)].copy()
                        if not flagged.empty:
                            def _build_flag_list(row: pd.Series) -> str:
                                labels = []
                                if row.get("flag_no_resources"):
                                    labels.append("no_resources")
                                if row.get("flag_low_quality"):
                                    labels.append("low_quality")
                                if row.get("flag_low_detail"):
                                    labels.append("low_detail")
                                if row.get("flag_generic"):
                                    labels.append("generic")
                                if row.get("flag_too_short"):
                                    labels.append("too_short")
                                if row.get("flag_location_mismatch"):
                                    labels.append("location_mismatch")
                                if row.get("flag_category_mismatch"):
                                    labels.append("category_mismatch")
                                if row.get("flag_not_actionable"):
                                    labels.append("not_actionable")
                                return ", ".join(labels)

                            flagged["flags"] = flagged.apply(_build_flag_list, axis=1)
                            flagged = flagged.sort_values("quality_score")

                            st.markdown("#### Flagged traces")
                            display_cols = [
                                "timestamp", "user", "query", "trace_type",
                                "quality_score", "resource_count",
                                "resource_detail_score", "flags",
                            ]
                            display_cols = [c for c in display_cols if c in flagged.columns]
                            st.dataframe(
                                flagged[display_cols].head(200),
                                use_container_width=True,
                                hide_index=True,
                            )

                            csv = flagged[display_cols].to_csv(index=False)
                            st.download_button(
                                "Download flagged traces (CSV)",
                                csv,
                                file_name="flagged_traces_detailed.csv",
                                mime="text/csv",
                                key="detailed_download",
                            )
                        else:
                            st.success("No traces failed the automated checks with current thresholds.")


# ---------------------------------------------------------------------------
# Landing page (shown before data is loaded)
# ---------------------------------------------------------------------------

def _show_landing_page():
    """Show a friendly getting-started page when no data is loaded."""
    st.markdown("---")
    st.markdown("### Welcome to the Phoenix PM Dashboard")
    st.markdown(
        "This dashboard gives you instant insights into your GenAI product's "
        "usage, performance, and quality — powered by Phoenix Arize traces."
    )

    st.markdown("#### Get started in 3 steps")

    col1, col2, col3 = st.columns(3)
    with col1:
        st.markdown(
            "**1. Paste your Phoenix URL**\n\n"
            "In the sidebar, paste either:\n"
            "- A base URL like `https://phoenix.example.com:6006`\n"
            "- Or a full spans URL like `.../projects/ABC/spans` "
            "(the project ID will be extracted automatically)"
        )
    with col2:
        st.markdown(
            "**2. Pick a time range**\n\n"
            "Choose Last 24 Hours, 7 Days, 30 Days, or a custom range. "
            "The dashboard will pull traces from that window."
        )
    with col3:
        st.markdown(
            "**3. Click Load Data**\n\n"
            "Hit the blue Load Data button. Once traces are loaded you will "
            "see Executive Summary, Usage Analytics, Performance Metrics, and more."
        )

    st.markdown("---")
    st.markdown(
        "**No config.yaml needed.** Everything that only requires trace data works out of the box. "
        "Features that need extra info (cohorts, meeting windows, locations) have inline UI inputs "
        "right where they live — no config file editing required.\n\n"
        "If you *do* have a `config.yaml`, it will pre-populate those UI inputs automatically."
    )


if __name__ == "__main__":
    main()
