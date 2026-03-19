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


BUILTIN_ZIP_TO_CITY = {
    # Austin, TX area
    '78701': 'Austin', '78702': 'Austin', '78703': 'Austin', '78704': 'Austin',
    '78705': 'Austin', '78712': 'Austin', '78721': 'Austin', '78722': 'Austin',
    '78723': 'Austin', '78724': 'Austin', '78725': 'Austin', '78726': 'Austin',
    '78727': 'Austin', '78728': 'Austin', '78729': 'Austin', '78730': 'Austin',
    '78731': 'Austin', '78732': 'Austin', '78733': 'Austin', '78734': 'Austin',
    '78735': 'Austin', '78736': 'Austin', '78737': 'Austin', '78738': 'Austin',
    '78739': 'Austin', '78741': 'Austin', '78742': 'Austin', '78744': 'Austin',
    '78745': 'Austin', '78746': 'Austin', '78747': 'Austin', '78748': 'Austin',
    '78749': 'Austin', '78750': 'Austin', '78751': 'Austin', '78752': 'Austin',
    '78753': 'Austin', '78754': 'Austin', '78756': 'Austin', '78757': 'Austin',
    '78758': 'Austin', '78759': 'Austin',
    # Surrounding TX cities
    '78613': 'Cedar Park', '78641': 'Leander', '78642': 'Leander',
    '78664': 'Round Rock', '78665': 'Round Rock', '78681': 'Round Rock',
    '78660': 'Pflugerville', '78626': 'Georgetown', '78628': 'Georgetown',
    '78633': 'Georgetown', '78610': 'Buda', '78640': 'Kyle', '78666': 'San Marcos',
    '78644': 'Lockhart', '78617': 'Del Valle', '78653': 'Manor', '78634': 'Hutto',
    '78654': 'Hutto', '78602': 'Bastrop', '78621': 'Elgin', '78645': 'Lago Vista',
    '76574': 'Taylor', '78669': 'Spicewood',
    # Pennsylvania (Keystone/Goodwill)
    '18102': 'Allentown, PA', '17602': 'Lancaster, PA', '19604': 'Reading, PA',
    '17101': 'Harrisburg, PA', '19601': 'Reading, PA', '17603': 'Lancaster, PA',
    # Virginia
    '23454': 'Virginia Beach, VA',
    # Other TX
    '77554': 'Galveston, TX', '75835': 'Palestine, TX', '78028': 'Kerrville, TX',
}


BUILTIN_CATEGORIES = {
    'Employment & Job Training': ['job', 'employment', 'career', 'work', 'hiring', 'resume', 'training program', 'workforce', 'vocational', 'apprentice', 'certification', 'cdl', 'cna'],
    'Housing & Shelter': ['housing', 'apartment', 'rent', 'shelter', 'homeless', 'eviction', 'mortgage', 'section 8', 'affordable housing', 'transitional housing'],
    'Food Assistance': ['food', 'meal', 'groceries', 'hunger', 'food bank', 'food pantry', 'snap', 'wic', 'nutrition'],
    'Financial Assistance': ['financial', 'money', 'debt', 'loan', 'credit', 'bankruptcy', 'bill', 'utility assistance', 'emergency funds', 'grants'],
    'Healthcare & Mental Health': ['health', 'medical', 'doctor', 'clinic', 'mental health', 'therapy', 'counseling', 'dental', 'vision', 'medication', 'insurance'],
    'Education & GED': ['education', 'school', 'ged', 'diploma', 'esl', 'english class', 'tutoring', 'college', 'scholarship', 'adult education'],
    'Transportation': ['transportation', 'bus', 'ride', 'transit', 'vehicle', 'gas', 'uber', 'lyft'],
    'Childcare & Family': ['childcare', 'child care', 'daycare', 'baby', 'parenting', 'family', 'preschool', 'after school'],
    'Legal Services': ['legal', 'lawyer', 'attorney', 'court', 'immigration', 'eviction defense', 'expungement'],
    'Substance Abuse': ['substance', 'addiction', 'drug', 'alcohol', 'rehab', 'recovery', 'sobriety', 'detox'],
    'Disability Services': ['disability', 'disabled', 'wheelchair', 'accessibility', 'special needs', 'ssdi', 'ssi'],
    'Veterans Services': ['veteran', 'military', 'va benefits', 'gi bill'],
}


def _match_keywords(query_lower: str, keywords: List[str]) -> int:
    """Score a query against a list of keywords. Multi-word keywords score higher."""
    score = 0
    for keyword in keywords:
        if keyword.lower() in query_lower:
            score += len(keyword.split())
    return score


def classify_query_category(
    query: str,
    config: DashboardConfig
) -> str:
    """Classify a query into a category based on configured keywords,
    falling back to builtin broad categories when config categories
    don't match."""
    if not query:
        return ""

    query_lower = query.lower()

    # Step 1: Try config-defined categories first
    best_category = ""
    best_score = 0

    if config.categories:
        for cat in config.categories:
            score = _match_keywords(query_lower, cat.keywords)
            if score > best_score:
                best_score = score
                best_category = cat.name

    if best_category:
        return best_category

    # Step 2: Fall back to builtin categories
    for cat_name, keywords in BUILTIN_CATEGORIES.items():
        score = _match_keywords(query_lower, keywords)
        if score > best_score:
            best_score = score
            best_category = cat_name

    return best_category
