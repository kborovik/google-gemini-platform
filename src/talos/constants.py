DEFAULT_CONTAINER = "credit-policies"
DEFAULT_APPLICATION_CONTAINER = "client-applications"
DEFAULT_APPLICATION_FIXTURES_RELATIVE = "tests/fixtures/client-applications"
DEFAULT_CORPUS = "kb-credit-policies"
DEFAULT_CHAT_MODEL = "gemini-3.8-flash"
DEFAULT_EMBEDDING_MODEL = "text-embedding-005"
DEFAULT_EMBEDDING_PUBLISHER_MODEL = "publishers/google/models/text-embedding-005"
DEFAULT_INSTRUCTIONS_RELATIVE = (
    "agents/credit_officer/credit-policy-agent.instructions.md"
)
DEFAULT_FACTS_RELATIVE = "corpus/facts.yaml"
DEFAULT_TEMPLATES_RELATIVE = "corpus/templates"
DEFAULT_OUTPUT_RELATIVE = "data/credit-policies"
DEFAULT_APPLICATION_OUTPUT_RELATIVE = "data/client-applications"
DEFAULT_APPLICATION_SCHEMA_RELATIVE = "corpus/application/schema.json"
DEFAULT_APPLICATION_SYSTEM_PROMPT_RELATIVE = "corpus/application/system.md"
DEFAULT_APPLICATION_USER_PROMPT_RELATIVE = "corpus/application/user.md.j2"
DEFAULT_APPLICATION_TEMPLATE_RELATIVE = "corpus/application/document.md.j2"

APPLICATION_TYPES = ("accepted", "rejected", "missing-data")
APPLICATION_ID_RE = r"^CA-\d{8}-\d+$"
APPLICATION_FILENAME_TEMPLATE = "credit-application-{application_id}.md"
FORBIDDEN_OUTCOME_TOKENS = (
    "ACCEPTED",
    "REJECTED",
    "MISSING",
    "APPROVED",
    "DECLINED",
    "DENIED",
    "PASS",
    "FAIL",
)
CUSTOMER_ID_RE = r"^SYN-\d{6}$"
EMAIL_DOMAIN = "example.invalid"
APPLICATION_LLM_ATTEMPTS = 2

PRODUCT_FAMILIES = (
    "residential_mortgage",
    "commercial_real_estate",
    "unsecured_consumer",
    "sme_lending",
    "construction_development",
)
SLOT_PRODUCT_FAMILY = {
    "accepted": "residential_mortgage",
    "rejected": "commercial_real_estate",
    "missing-data": "sme_lending",
}
PRODUCT_FAMILY_LABEL = {
    "residential_mortgage": "owner-occupied residential mortgage",
    "commercial_real_estate": "stabilized commercial real estate",
    "unsecured_consumer": "unsecured personal loan",
    "sme_lending": "SME working-capital facility",
    "construction_development": "construction and development facility",
}
PRODUCT_REQUIRED_DOCUMENTS = {
    "residential_mortgage": (
        "last 2 pay stubs",
        "W-2",
        "residential appraisal",
    ),
    "commercial_real_estate": (
        "2 years tax returns",
        "YTD P&L",
        "CRE valuation",
    ),
    "unsecured_consumer": (
        "last 2 pay stubs",
        "W-2",
    ),
    "sme_lending": (
        "personal guarantee",
        "last 2 pay stubs",
        "W-2",
    ),
    "construction_development": (
        "completion guarantee",
        "2 years tax returns",
        "YTD P&L",
    ),
}
PRODUCT_REQUIRED_FACTS: dict[str, tuple[str, ...]] = {
    "residential_mortgage": ("appraisal_date", "licensed_appraiser"),
    "commercial_real_estate": ("valuation_date",),
    "unsecured_consumer": (),
    "sme_lending": (),
    "construction_development": ("valuation_date",),
}
PRODUCT_FACILITY_LIMITS: dict[str, tuple[tuple[str, str, float], ...]] = {
    "residential_mortgage": (
        ("ltv", "<=", 80.0),
        ("dti", "<=", 43.0),
        ("credit_score", ">=", 680.0),
    ),
    "commercial_real_estate": (
        ("ltv", "<=", 65.0),
        ("dscr", ">=", 1.25),
    ),
    "unsecured_consumer": (
        ("loan_amount", "<=", 50_000.0),
        ("credit_score", ">=", 700.0),
        ("dti", "<=", 36.0),
        ("tenor_months", "<=", 60.0),
    ),
    "sme_lending": (
        ("loan_amount", "<=", 2_500_000.0),
        ("years_in_operation", ">=", 3.0),
        ("tenor_months", "<=", 12.0),
    ),
    "construction_development": (
        ("ltv", "<=", 55.0),
        ("tenor_months", "<=", 18.0),
    ),
}
FACILITY_PROMPT_HINTS = {
    "residential_mortgage": (
        "Owner-occupied residential: max LTV 80%, max DTI 43%, min credit score 680. "
        "Include facility.appraisal_date as YYYY-MM-DD within the last 90 days and "
        "licensed_appraiser yes (AVM only if LTV ≤ 60% and loan ≤ USD 400,000)."
    ),
    "commercial_real_estate": (
        "Stabilized CRE: max LTV 65%, min DSCR 1.25x. "
        "Include facility.valuation_date as YYYY-MM-DD within the last 120 days."
    ),
    "unsecured_consumer": (
        "Unsecured personal loan: max USD 50,000, max term 60 months, min FICO 700, "
        "max DTI 36%."
    ),
    "sme_lending": (
        "SME working capital: max USD 2,500,000, personal guarantee above USD 250,000, "
        "max tenor 12 months, min 3 years in operation. Amount must be above USD 250,000."
    ),
    "construction_development": (
        "Construction: construction max LTV 55%, max interest-only 18 months, "
        "completion guarantee required."
    ),
}

WATERMARK = "SYNTHETIC — DEMO ONLY"
APPLICATION_DISCLAIMER_PHRASES = (
    WATERMARK,
    "synthetic credit application",
    "This is not a real borrower record",
)

CORPUS_IDS = (
    "CP-RML-2026-01",
    "CP-CRE-2026-01",
    "CP-UCL-2026-01",
    "CP-SME-2026-01",
    "CP-EXC-2026-01",
    "CP-PRO-2026-01",
    "CP-COL-2026-01",
    "CP-DOC-2026-01",
    "CP-RPL-2026-01",
    "CP-ESG-2026-01",
    "CP-CND-2026-01",
    "CP-AUTH-2026-01",
)

GOLDEN_QUERY_IDS = (
    "Q-RML-LTV-OO",
    "Q-RML-DTI",
    "Q-CRE-DSCR",
    "Q-UCL-MAX",
    "Q-SME-PG",
    "Q-COL-AVM",
    "Q-AUTH-RM",
    "Q-CMP-LTV-CRE-ESG",
    "Q-CMP-CONSTRUCTION",
    "Q-NEG-AUTO",
    "Q-NEG-SOVEREIGN",
    "Q-AMB-LTV",
    "Q-CIT-PROHIBITED",
    "Q-MT-EXCEPTION",
    "Q-MT-ESG-FOLLOWUP",
    "Q-ADV-JAILBREAK",
    "Q-ADV-INVENT",
    "Q-DOC-SE",
)

DEFAULT_GOLDEN_LAYERS = ("retrieval", "agent")
REFUSAL_SENTENCE = "not in the published policies"
TEAMS_CHECKLIST_QUERY_IDS = (
    "Q-RML-LTV-OO",
    "Q-NEG-AUTO",
    "Q-ADV-JAILBREAK",
)

MIN_INDEXED_ITEMS = 12
MIN_APPLICATION_INDEXED_ITEMS = 3
WAIT_TIMEOUT_SECONDS = 15 * 60
POLL_INTERVAL_SECONDS = 10.0
CHUNK_SIZE = 512
CHUNK_OVERLAP = 100

REQUIRED_ENV = (
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_CLOUD_LOCATION",
    "GCS_BUCKET",
)

CANONICAL_ENV = (
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_CLOUD_LOCATION",
    "GCS_BUCKET",
    "GCS_URI",
)

CORPUS_DESCRIPTION = (
    "Contoso Demo Bank credit policies and client applications. "
    "Use credit-policies for LTV, DTI, DSCR, collateral, exceptions, prohibited "
    "sectors, documentation, construction, and ESG. Use client-applications when "
    "evaluating a filing by application_id or customer_name."
)
