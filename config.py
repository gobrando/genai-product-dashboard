"""
Configuration loader for the Phoenix PM Dashboard.
Reads a YAML config file that defines product-specific settings:
cohorts, meeting windows, locations, excluded users, trace type mappings, etc.
"""
import yaml
import os
import re
import logging
from typing import Dict, List, Optional, Set
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

CONFIG_PATH = os.getenv("DASHBOARD_CONFIG", "config.yaml")


@dataclass
class CohortConfig:
    name: str
    emails: List[str] = field(default_factory=list)
    color: str = ""


@dataclass
class LocationConfig:
    name: str
    domains: List[str] = field(default_factory=list)
    cohort_names: List[str] = field(default_factory=list)


@dataclass
class MeetingSessionConfig:
    name: str
    starts: List[str] = field(default_factory=list)


@dataclass
class MeetingWindowsConfig:
    timezone: str = "America/New_York"
    duration_minutes: int = 60
    sessions: List[MeetingSessionConfig] = field(default_factory=list)
    always_organic_emails: List[str] = field(default_factory=list)


@dataclass
class TraceTypeMapping:
    pattern: str
    type_name: str


@dataclass
class GeographicRegion:
    name: str
    zip_codes: List[str] = field(default_factory=list)


@dataclass
class CategoryConfig:
    name: str
    keywords: List[str] = field(default_factory=list)


@dataclass
class DashboardConfig:
    product_name: str = "My AI Product"

    # User exclusion
    excluded_emails: Set[str] = field(default_factory=set)
    excluded_domain_patterns: List[str] = field(default_factory=list)

    # Email domain prioritization for extraction
    priority_email_domains: List[str] = field(default_factory=list)

    # Trace type mappings (root span name pattern -> type label)
    trace_type_mappings: List[TraceTypeMapping] = field(default_factory=list)

    # Cohorts
    cohorts: List[CohortConfig] = field(default_factory=list)

    # Locations (domain-based grouping)
    locations: List[LocationConfig] = field(default_factory=list)

    # Meeting windows for organic usage filtering
    meeting_windows: MeetingWindowsConfig = field(default_factory=MeetingWindowsConfig)

    # Geographic regions
    geographic_regions: List[GeographicRegion] = field(default_factory=list)
    zip_to_city: Dict[str, str] = field(default_factory=dict)

    # Category keywords for query classification
    categories: List[CategoryConfig] = field(default_factory=list)

    # Secondary Phoenix source
    secondary_phoenix_url: str = ""
    secondary_project_id: str = ""


def load_config(path: Optional[str] = None) -> DashboardConfig:
    """Load configuration from YAML file. Returns defaults if file not found."""
    config_path = path or CONFIG_PATH

    if not os.path.exists(config_path):
        logger.info(f"No config file found at {config_path}, using defaults")
        return DashboardConfig()

    with open(config_path, 'r') as f:
        raw = yaml.safe_load(f) or {}

    config = DashboardConfig()

    # Product name
    product = raw.get('product', {})
    config.product_name = product.get('name', config.product_name)

    # Secondary source
    secondary = product.get('secondary_source', {})
    config.secondary_phoenix_url = secondary.get('api_url', '')
    config.secondary_project_id = secondary.get('project_id', '')

    # Excluded users
    excluded = raw.get('excluded_users', {})
    config.excluded_emails = {
        e.lower().strip()
        for e in excluded.get('emails', [])
    }
    config.excluded_domain_patterns = excluded.get('domain_patterns', [])

    # Priority email domains
    config.priority_email_domains = raw.get('priority_email_domains', [])

    # Trace type mappings
    for mapping in raw.get('trace_types', []):
        config.trace_type_mappings.append(TraceTypeMapping(
            pattern=mapping.get('pattern', ''),
            type_name=mapping.get('type', 'other')
        ))

    # Cohorts
    for cohort in raw.get('cohorts', []):
        config.cohorts.append(CohortConfig(
            name=cohort.get('name', 'Unnamed'),
            emails=[e.strip().lower() for e in cohort.get('emails', [])],
            color=cohort.get('color', '')
        ))

    # Locations
    for loc in raw.get('locations', []):
        config.locations.append(LocationConfig(
            name=loc.get('name', 'Unknown'),
            domains=[d.lower() for d in loc.get('domains', [])],
            cohort_names=loc.get('cohorts', [])
        ))

    # Meeting windows
    mw = raw.get('meeting_windows', {})
    config.meeting_windows = MeetingWindowsConfig(
        timezone=mw.get('timezone', 'America/New_York'),
        duration_minutes=mw.get('duration_minutes', 60),
        always_organic_emails=[
            e.lower().strip() for e in mw.get('always_organic_emails', [])
        ],
    )
    for session in mw.get('sessions', []):
        config.meeting_windows.sessions.append(MeetingSessionConfig(
            name=session.get('name', 'Unnamed Session'),
            starts=session.get('starts', [])
        ))

    # Geographic regions
    geo = raw.get('geographic', {})
    for region in geo.get('regions', []):
        config.geographic_regions.append(GeographicRegion(
            name=region.get('name', 'Unknown'),
            zip_codes=region.get('zip_codes', [])
        ))
    config.zip_to_city = geo.get('zip_to_city', {})

    # Categories
    for cat in raw.get('categories', []):
        config.categories.append(CategoryConfig(
            name=cat.get('name', 'Other'),
            keywords=cat.get('keywords', [])
        ))

    return config


def get_all_meeting_starts(config: DashboardConfig) -> List[str]:
    """Flatten all meeting session starts into a single list."""
    starts = []
    for session in config.meeting_windows.sessions:
        starts.extend(session.starts)
    return starts


def get_cohort_emails(config: DashboardConfig, cohort_name: str) -> Set[str]:
    """Get email set for a named cohort."""
    for cohort in config.cohorts:
        if cohort.name == cohort_name:
            return {e.lower().strip() for e in cohort.emails}
    return set()


def get_all_cohort_emails(config: DashboardConfig) -> Set[str]:
    """Get all cohort emails combined."""
    emails = set()
    for cohort in config.cohorts:
        emails.update(e.lower().strip() for e in cohort.emails)
    return emails


def get_location_for_email(
    config: DashboardConfig,
    email: str,
    cohort_email_sets: Optional[Dict[str, Set[str]]] = None
) -> str:
    """Determine location label for an email based on config."""
    email_lower = email.lower().strip()

    if not email_lower or 'unknown' in email_lower or '@' not in email_lower:
        return "Unknown"

    # Check each location's domain patterns and associated cohorts
    for loc in config.locations:
        # Domain match
        for domain in loc.domains:
            if domain in email_lower:
                return loc.name

        # Cohort match
        if cohort_email_sets and loc.cohort_names:
            for cohort_name in loc.cohort_names:
                if cohort_name in cohort_email_sets:
                    if email_lower in cohort_email_sets[cohort_name]:
                        return loc.name

    return "Other"


def get_region_zip_set(config: DashboardConfig) -> Set[str]:
    """Get the combined set of all configured region zip codes."""
    zips = set()
    for region in config.geographic_regions:
        zips.update(region.zip_codes)
    return zips


def classify_trace_type(
    root_span_name: str,
    config: DashboardConfig
) -> str:
    """Classify a trace type based on configured pattern mappings."""
    name_lower = root_span_name.lower()
    for mapping in config.trace_type_mappings:
        if re.search(mapping.pattern, name_lower):
            return mapping.type_name
    return "other"


def classify_query_category(
    query: str,
    config: DashboardConfig
) -> str:
    """Classify a query into a category based on configured keywords."""
    if not query or not config.categories:
        return ""

    query_lower = query.lower()
    best_category = ""
    best_score = 0

    for cat in config.categories:
        score = 0
        for keyword in cat.keywords:
            if keyword.lower() in query_lower:
                score += len(keyword.split())
        if score > best_score:
            best_score = score
            best_category = cat.name

    return best_category
