"""Hotel operations dataset configuration - property profiles, pools, and constants.

Central configuration module for the LUMI dataset generator. Contains all static
lookup tables, property profiles, VIP name pools, room type distributions, and
channel/category constants used by the individual generator modules.

All data is deterministic (no randomness). Variance in generated data comes from
day-of-week modulo patterns, property-specific offset arrays, and hash-based
rotation through these pools.

Supports REQ-DS-2 through REQ-DS-6 and REQ-DS-8 (deterministic generation).
"""

from decimal import Decimal
from typing import Any, Dict, List, Tuple


# ---------------------------------------------------------------------------
# Room Type Distribution Constants
# ---------------------------------------------------------------------------

# Percentage distribution of room types across all properties.
# Applied to each property's totalRooms to determine inventory breakdown.
ROOM_TYPE_DISTRIBUTION: Dict[str, float] = {
    "SUITE": 0.03,
    "PENTHOUSE": 0.02,
    "KING_DELUXE": 0.15,
    "QUEEN_DELUXE": 0.15,
    "KING_STANDARD": 0.65,
}


# Floor assignment: room types are grouped by floor ranges.
# Each tuple is (start_floor, end_floor) inclusive. The rooms_generator
# distributes rooms evenly across these floors per type.
ROOM_TYPE_FLOOR_RANGES: Dict[str, Tuple[int, int]] = {
    "PENTHOUSE": (20, 24),
    "SUITE": (18, 22),
    "KING_DELUXE": (12, 17),
    "QUEEN_DELUXE": (8, 14),
    "KING_STANDARD": (2, 11),
}

# Views available per property - assigned based on floor and room position.
# Higher floors and even-numbered rooms get premium views.
VIEWS: List[str] = ["LAKE", "CITY", "PARK", "GARDEN", "NONE"]

# Property-specific view assignments (which views are available at each property)
PROPERTY_VIEWS: Dict[str, List[str]] = {
    "ALOHA-CHI-001": ["LAKE", "CITY", "PARK", "NONE"],
    "ALOHA-MIA-001": ["CITY", "GARDEN", "PARK", "NONE"],
    "ALOHA-TYO-001": ["CITY", "PARK", "GARDEN", "NONE"],
    "ALOHA-MAD-001": ["CITY", "PARK", "GARDEN", "NONE"],
    "ALOHA-BOM-001": ["CITY", "GARDEN", "PARK", "NONE"],
}


# ---------------------------------------------------------------------------
# Property Profiles
# ---------------------------------------------------------------------------

# Each profile defines a pilot property's characteristics for data generation.
# Occupancy tuples are (min_pct, max_pct) for that day-type pattern.
# ADR baselines are in local currency (USD, EUR, JPY equiv in USD, INR equiv).
# businessWeight: 0.0 = pure leisure, 1.0 = pure business travel.
# eventCalendar: list of events with ISO week numbers that trigger group blocks.

PROPERTY_PROFILES: List[Dict[str, Any]] = [
    {
        "propertyId": "ALOHA-CHI-001",
        "name": "Aloha Grand Chicago",
        "totalRooms": 368,
        "currency": "USD",
        "weekdayOccupancy": (85, 92),
        "weekendOccupancy": (72, 80),
        "adrBaseline": Decimal("245"),
        "businessWeight": 0.75,
        "budgetOccupancy": 83,
        "budgetAdr": Decimal("240"),
        "eventCalendar": [
            {"weekNumber": 2, "name": "Midwest Tech Summit", "rooms": 95, "nights": 3},
            {"weekNumber": 3, "name": "Meridian Corp Annual", "rooms": 82, "nights": 2},
            {"weekNumber": 4, "name": "FinServ Leaders Forum", "rooms": 65, "nights": 3},
        ],
    },
    {
        "propertyId": "ALOHA-MIA-001",
        "name": "Aloha Resort & Spa Miami",
        "totalRooms": 425,
        "currency": "USD",
        "weekdayOccupancy": (75, 82),
        "weekendOccupancy": (85, 92),
        "adrBaseline": Decimal("278"),
        "businessWeight": 0.30,
        "budgetOccupancy": 80,
        "budgetAdr": Decimal("270"),
        "eventCalendar": [
            {"weekNumber": 1, "name": "Luxury Travel Expo", "rooms": 110, "nights": 4},
            {"weekNumber": 3, "name": "Caribbean Wellness Retreat", "rooms": 55, "nights": 3},
        ],
    },
    {
        "propertyId": "ALOHA-TYO-001",
        "name": "Aloha Grand Tokyo",
        "totalRooms": 480,
        "currency": "JPY",
        "weekdayOccupancy": (88, 96),
        "weekendOccupancy": (88, 94),
        "adrBaseline": Decimal("195"),
        "businessWeight": 0.80,
        "budgetOccupancy": 90,
        "budgetAdr": Decimal("190"),
        "eventCalendar": [
            {"weekNumber": 1, "name": "Asia Pacific Tech Conference", "rooms": 120, "nights": 3},
            {"weekNumber": 2, "name": "Japan Hospitality Forum", "rooms": 75, "nights": 2},
            {"weekNumber": 4, "name": "Global Finance Summit Tokyo", "rooms": 90, "nights": 3},
        ],
    },
    {
        "propertyId": "ALOHA-MAD-001",
        "name": "Aloha Resort & Spa Madrid",
        "totalRooms": 380,
        "currency": "EUR",
        "weekdayOccupancy": (72, 80),
        "weekendOccupancy": (82, 88),
        "adrBaseline": Decimal("188"),
        "businessWeight": 0.40,
        "budgetOccupancy": 76,
        "budgetAdr": Decimal("180"),
        "eventCalendar": [
            {"weekNumber": 2, "name": "European Luxury Travel Awards", "rooms": 60, "nights": 3},
            {"weekNumber": 4, "name": "Madrid Fashion Week Partners", "rooms": 45, "nights": 4},
        ],
    },
    {
        "propertyId": "ALOHA-BOM-001",
        "name": "Aloha Resort & Spa Mumbai",
        "totalRooms": 355,
        "currency": "INR",
        "weekdayOccupancy": (80, 88),
        "weekendOccupancy": (78, 85),
        "adrBaseline": Decimal("138"),
        "businessWeight": 0.65,
        "budgetOccupancy": 82,
        "budgetAdr": Decimal("130"),
        "eventCalendar": [
            {"weekNumber": 1, "name": "India Startup Summit", "rooms": 70, "nights": 3},
            {"weekNumber": 3, "name": "Bollywood Awards Gala", "rooms": 95, "nights": 2},
            {"weekNumber": 4, "name": "South Asia Trade Expo", "rooms": 50, "nights": 3},
        ],
    },
]


# Quick lookup: propertyId -> profile dict for O(1) access in generators
PROPERTY_LOOKUP: Dict[str, Dict[str, Any]] = {
    p["propertyId"]: p for p in PROPERTY_PROFILES
}

# Ordered list of property IDs for deterministic iteration
PROPERTY_IDS: List[str] = [p["propertyId"] for p in PROPERTY_PROFILES]

# Number of days of historical data to generate
SEED_DAYS: int = 30


# ---------------------------------------------------------------------------
# VIP Name Pool (250 names - 50 per property)
# ---------------------------------------------------------------------------

# Internationally diverse guest names grouped by property assignment.
# Each property's 50 guests draw from names representing the cultural
# backgrounds common to that hotel's guest demographics.

VIP_NAME_POOL: Dict[str, List[str]] = {
    # Chicago: mix of American, European, East Asian, South Asian business travelers
    "ALOHA-CHI-001": [
        "David Chen", "Sarah Williams", "Robert Kim", "Elena Vasquez",
        "James Park", "Maria Santos", "Thomas Mueller", "Jennifer Liu",
        "Michael O'Brien", "Aisha Patel", "Christopher Yang", "Rachel Goldman",
        "Andrew Nakamura", "Patricia Reyes", "Daniel Kowalski", "Susan Chang",
        "William Okafor", "Margaret Lindqvist", "Richard Gupta", "Katherine Ross",
        "Jonathan Haddad", "Laura Chen-Ramirez", "Benjamin Ito", "Samantha Brooks",
        "Gregory Fernandez", "Angela Morrison", "Steven Watanabe", "Diana Petrov",
        "Marcus Johnson", "Olivia Tanaka", "Nathan Singh", "Emily Richardson",
        "George Alvarez", "Victoria Schwartz", "Kevin Zhao", "Michelle DuBois",
        "Ryan Kapoor", "Hannah Bergstrom", "Philip Adeyemi", "Christine Park",
        "Edward Sullivan", "Sophia Mendez", "Brian Yamamoto", "Alexandra Volkov",
        "Timothy Nguyen", "Rebecca Blackwood", "Douglas Ibrahim", "Natalie Costa",
        "Jeffrey Takahashi", "Catherine Laurent",
    ],
    # Miami: Latin American, Caribbean, American leisure, European travelers
    "ALOHA-MIA-001": [
        "Carlos Mendoza", "Isabella Martinez", "Roberto Silva", "Ana Lucia Vargas",
        "Diego Fernandez", "Valentina Rojas", "Fernando Costa", "Camila Herrera",
        "Sebastian Morales", "Gabriela Ortiz", "Miguel Angel Torres", "Sofia Delgado",
        "Juan Pablo Rivera", "Daniela Castillo", "Alejandro Reyes", "Lucia Paredes",
        "Rafael Guerrero", "Natalia Vega", "Pablo Escobar-Ruiz", "Mariana Aguilar",
        "Luis Hernandez", "Andrea Salazar", "Jose Antonio Diaz", "Carmen Fuentes",
        "Francisco Navarro", "Teresa Molina", "Andres Cardenas", "Patricia Romero",
        "Ricardo Medina", "Adriana Pena", "Oscar Jimenez", "Monica Gutierrez",
        "Enrique Ramirez", "Beatriz Lozano", "Jorge Villanueva", "Claudia Serrano",
        "Alberto Cruz", "Rosa Maria Leon", "Guillermo Soto", "Silvia Marquez",
        "Eduardo Flores", "Veronica Acosta", "Santiago Duarte", "Alma Contreras",
        "Hector Salinas", "Irene Vasquez", "Raul Dominguez", "Gloria Pacheco",
        "Martin Cabrera", "Yolanda Estrada",
    ],
    # Tokyo: Japanese, Korean, Chinese, Southeast Asian, some Western business travelers
    "ALOHA-TYO-001": [
        "Takeshi Yamamoto", "Yuki Tanaka", "Hiroshi Nakamura", "Sakura Watanabe",
        "Kenji Suzuki", "Akiko Sato", "Masashi Kobayashi", "Rina Matsumoto",
        "Daisuke Hashimoto", "Mika Yoshida", "Shota Inoue", "Ayumi Fujimoto",
        "Jun-ho Park", "Min-ji Kim", "Sang-woo Lee", "Hye-jin Choi",
        "Wei Zhang", "Mei-ling Wang", "Li Wei Chen", "Xiao-yu Liu",
        "Thaksin Panyarachun", "Siriwan Channarong", "Nguyen Van Minh", "Tran Thi Lan",
        "Rajesh Krishnamurthy", "Deepa Venkatesh", "Oliver Hutchinson", "Charlotte Reed",
        "Hans Becker", "Ingrid Johansson", "Ryota Kimura", "Nozomi Hayashi",
        "Kohei Mori", "Yui Shimizu", "Takuya Abe", "Haruka Ogawa",
        "Kaito Okamoto", "Nanami Ueda", "Riku Sasaki", "Hina Maeda",
        "Sora Murata", "Misaki Honda", "Aoi Kondo", "Riko Fukuda",
        "Hayato Arai", "Momoka Endo", "Daiki Ishida", "Kanon Saito",
        "Soma Nishimura", "Hinata Kawaguchi",
    ],
    # Madrid: Spanish, European, Middle Eastern, North African travelers
    "ALOHA-MAD-001": [
        "Alejandro Ruiz", "Isabel Navarro", "Javier Moreno", "Carmen Delgado",
        "Pablo Fernandez", "Maria Teresa Gomez", "Antonio Serrano", "Laura Jimenez",
        "Manuel Ortiz", "Pilar Ramirez", "Francisco Garcia", "Elena Herrero",
        "Pierre Lefebvre", "Sophie Martin", "Marco Bianchi", "Giulia Rossi",
        "Klaus Hofmann", "Brigitte Schneider", "Henrik Larsson", "Astrid Andersen",
        "Mohammed Al-Rashid", "Fatima Al-Sayed", "Khalid Mansour", "Leila Bakhtiari",
        "Omar El-Masri", "Nadia Khoury", "Youssef Benali", "Amira Haddad",
        "Stefan Novak", "Katarina Dvorak", "Dimitri Volkov", "Tatiana Sokolova",
        "Rodrigo Almeida", "Beatriz Carvalho", "Hugo Ferreira", "Ines Martins",
        "Felipe Castillo", "Adriana Vega", "Sergio Fuentes", "Cristina Molina",
        "Raul Herrera", "Victoria Lozano", "Ignacio Torres", "Lucia Blanco",
        "Alvaro Guerrero", "Marta Calleja", "Diego Prieto", "Ana Belen Ramos",
        "Gonzalo Medina", "Rocio Sanchez",
    ],
    # Mumbai: Indian, Middle Eastern, Southeast Asian, Western business travelers
    "ALOHA-BOM-001": [
        "Rajesh Sharma", "Priya Patel", "Vikram Malhotra", "Ananya Reddy",
        "Arjun Kapoor", "Deepika Nair", "Sanjay Gupta", "Kavita Iyer",
        "Rohit Mehta", "Neha Joshi", "Aditya Chatterjee", "Sunita Rao",
        "Venkatesh Subramanian", "Lakshmi Krishnan", "Ashok Banerjee", "Meera Deshmukh",
        "Suresh Pillai", "Anjali Saxena", "Rahul Verma", "Pooja Agarwal",
        "Manish Tiwari", "Ritu Singh", "Karthik Narayanan", "Divya Bhatia",
        "Amitabh Chandra", "Swati Kulkarni", "Gaurav Pandey", "Nandini Menon",
        "Vivek Srinivasan", "Preeti Kaul", "Harish Gopalan", "Asha Hegde",
        "Mohammed Ismail", "Zara Ahmed", "Faisal Khan", "Ayesha Siddiqui",
        "Chen Wei-lin", "Park Soo-yeon", "Hideki Matsuda", "Sarah Mitchell",
        "James Robertson", "Emma Sullivan", "Alexander Petrov", "Olga Kuznetsova",
        "Ahmad Al-Farsi", "Layla Hassan", "Prakash Jha", "Seema Arora",
        "Nitin Deshpande", "Shreya Mukherjee",
    ],
}


# ---------------------------------------------------------------------------
# Loyalty Tier Distribution (per property, 50 guests total)
# ---------------------------------------------------------------------------

# Fixed distribution of loyalty tiers per property.
# Ambassador: highest tier (15), Titanium: mid-high (20), Platinum: mid (15)
LOYALTY_TIER_DISTRIBUTION: Dict[str, int] = {
    "AMBASSADOR": 15,
    "TITANIUM": 20,
    "PLATINUM": 15,
}

# Stay count ranges by loyalty tier - Ambassador guests have many more visits.
# Tuple: (min_stays, max_stays)
STAY_COUNT_RANGES: Dict[str, Tuple[int, int]] = {
    "AMBASSADOR": (25, 80),
    "TITANIUM": (10, 40),
    "PLATINUM": (5, 20),
}


# ---------------------------------------------------------------------------
# Guest Preferences Pool
# ---------------------------------------------------------------------------

# Available preferences for VIP guest profiles. Each guest receives 1-5
# preferences deterministically assigned based on their index in the pool.
GUEST_PREFERENCES_POOL: List[str] = [
    "HIGH_FLOOR",
    "LOW_FLOOR",
    "QUIET_FLOOR",
    "FEATHER_FREE",
    "HYPOALLERGENIC",
    "EXTRA_PILLOWS",
    "FIRM_MATTRESS",
    "CHAMPAGNE_ARRIVAL",
    "FRUIT_BASKET",
    "NEWSPAPER_DELIVERY",
    "EARLY_CHECK_IN",
    "LATE_CHECKOUT",
    "ADJOINING_ROOMS",
    "ALLERGY_FREE_ROOM",
    "SPECIFIC_FLOOR",
    "OCEAN_VIEW",
    "CITY_VIEW",
]


# ---------------------------------------------------------------------------
# Special Occasions Pool
# ---------------------------------------------------------------------------

# Occasions that can be attached to a guest's arrival (10% of guests per day).
# Deterministically assigned based on guest index and day offset.
SPECIAL_OCCASIONS_POOL: List[str] = [
    "ANNIVERSARY",
    "BIRTHDAY",
    "HONEYMOON",
    "WEDDING",
    "RETIREMENT",
    "GRADUATION",
    "CORPORATE_RETREAT",
]


# ---------------------------------------------------------------------------
# Sensitive Notes Pool (5% of guests)
# ---------------------------------------------------------------------------

# Confidential notes for high-profile guests requiring special handling.
# Only attached to ~5% of guest profiles (2-3 per property).
SENSITIVE_NOTES_POOL: List[str] = [
    "Celebrity - requires extra privacy and discrete check-in",
    "Past complaint - noise issue room 1202, avoid adjacent rooms",
    "Medical condition - ground floor preferred, ADA accessible",
    "VIP Investor - board presentation scheduled during stay",
    "Board Member - corporate governance meetings this visit",
    "Diplomatic guest - security protocol required",
    "Allergy alert - severe nut allergy, kitchen must be notified",
    "Media personality - no photography policy enforcement needed",
    "Repeat service recovery - last stay had delayed room service",
    "High-value corporate account - personal attention from GM requested",
]


# ---------------------------------------------------------------------------
# Channel Distribution Constants
# ---------------------------------------------------------------------------

# Booking channel percentages for reservation generation.
# These weights determine how reservations are distributed across channels.
CHANNEL_DISTRIBUTION: Dict[str, float] = {
    "DIRECT": 0.30,
    "OTA": 0.25,
    "CORPORATE": 0.20,
    "GROUP": 0.15,
    "LOYALTY": 0.10,
}

# Ordered channel list for deterministic assignment via modulo indexing
CHANNEL_ORDER: List[str] = ["DIRECT", "OTA", "CORPORATE", "GROUP", "LOYALTY"]

# Cumulative thresholds for channel assignment (used by reservations_generator)
# e.g., index 0-29 = DIRECT, 30-54 = OTA, 55-74 = CORPORATE, etc.
CHANNEL_CUMULATIVE_THRESHOLDS: List[Tuple[str, int]] = [
    ("DIRECT", 30),
    ("OTA", 55),
    ("CORPORATE", 75),
    ("GROUP", 90),
    ("LOYALTY", 100),
]


# ---------------------------------------------------------------------------
# Work Order Categories Pool
# ---------------------------------------------------------------------------

# Issue types for work order generation with associated default priorities.
# Priority can be overridden by the generator based on room premium status.
WORK_ORDER_CATEGORIES: List[Dict[str, str]] = [
    {"issueType": "HVAC", "defaultPriority": "HIGH"},
    {"issueType": "PLUMBING", "defaultPriority": "HIGH"},
    {"issueType": "ELECTRICAL", "defaultPriority": "CRITICAL"},
    {"issueType": "HOUSEKEEPING", "defaultPriority": "MEDIUM"},
    {"issueType": "STRUCTURAL", "defaultPriority": "LOW"},
    {"issueType": "IT_NETWORK", "defaultPriority": "MEDIUM"},
    {"issueType": "ELEVATOR", "defaultPriority": "CRITICAL"},
]

# Work order resolution time ranges in hours by priority level.
# Tuple: (min_hours, max_hours) for the status lifecycle.
RESOLUTION_TIME_HOURS: Dict[str, Tuple[int, int]] = {
    "CRITICAL": (6, 12),
    "HIGH": (12, 24),
    "MEDIUM": (24, 48),
    "LOW": (48, 72),
}

# Maintenance team member names for work order assignment rotation
MAINTENANCE_TEAM: List[str] = [
    "Carlos Rivera", "Tomoko Saitoh", "Ahmed Hassan", "Priya Sharma",
    "Michael Torres", "Yuki Nakagawa", "David Okonkwo", "Anita Desai",
    "Robert Singh", "Maria Gonzalez", "Kenji Takahashi", "Fatima Al-Rashid",
]


# ---------------------------------------------------------------------------
# Stay Length Weights by Property Type
# ---------------------------------------------------------------------------

# Weighted stay lengths (in nights) for reservation generation.
# Business-heavy properties favor shorter stays (1-2 nights).
# Leisure-heavy properties favor longer stays (2-4 nights).
# Index 0 = 1 night, index 1 = 2 nights, etc. Values are relative weights.

STAY_LENGTH_WEIGHTS_BUSINESS: List[int] = [40, 30, 15, 10, 5]
STAY_LENGTH_WEIGHTS_LEISURE: List[int] = [15, 25, 30, 20, 10]

# Mapping: each property gets a blended weight array based on businessWeight.
# Precomputed at module load for performance in the reservations generator.


def _compute_stay_weights(business_weight: float) -> List[int]:
    """Compute blended stay length weights from business/leisure ratio.

    Linearly interpolates between business and leisure weight arrays
    based on the property's businessWeight factor.

    Args:
        business_weight: Float between 0.0 (pure leisure) and 1.0 (pure business).

    Returns:
        List of 5 integer weights for stay lengths 1-5 nights.
    """
    weights: List[int] = []
    for i in range(5):
        blended = (
            business_weight * STAY_LENGTH_WEIGHTS_BUSINESS[i]
            + (1.0 - business_weight) * STAY_LENGTH_WEIGHTS_LEISURE[i]
        )
        weights.append(round(blended))
    return weights


# Precomputed stay weights per property for O(1) lookup
PROPERTY_STAY_WEIGHTS: Dict[str, List[int]] = {
    profile["propertyId"]: _compute_stay_weights(profile["businessWeight"])
    for profile in PROPERTY_PROFILES
}


# ---------------------------------------------------------------------------
# Rate Code Distribution
# ---------------------------------------------------------------------------

# Rate codes for reservations, correlated with channel type.
# Each channel has a primary rate code assignment.
CHANNEL_TO_RATE_CODE: Dict[str, str] = {
    "DIRECT": "BAR",
    "OTA": "BAR",
    "CORPORATE": "CORP",
    "GROUP": "GROUP",
    "LOYALTY": "LOYALTY",
}

# Rate multipliers by room type (relative to property ADR baseline)
ROOM_TYPE_RATE_MULTIPLIERS: Dict[str, Decimal] = {
    "PENTHOUSE": Decimal("3.50"),
    "SUITE": Decimal("2.50"),
    "KING_DELUXE": Decimal("1.40"),
    "QUEEN_DELUXE": Decimal("1.20"),
    "KING_STANDARD": Decimal("1.00"),
}


# ---------------------------------------------------------------------------
# Corporate Account Names Pool
# ---------------------------------------------------------------------------

# Company names for CORPORATE account type guests and group bookings.
# Deterministically assigned based on guest/reservation index.
CORPORATE_ACCOUNTS: List[str] = [
    "Meridian Corp",
    "Atlas Technologies",
    "Horizon Partners",
    "Vanguard Solutions",
    "Sterling & Associates",
    "Pacific Ventures",
    "Nexus Global",
    "Pinnacle Industries",
    "Summit Healthcare",
    "Cascade Financial",
    "Evergreen Consulting",
    "Sapphire Holdings",
    "Citadel Logistics",
    "Monarch Pharmaceuticals",
    "Zenith Aerospace",
]


# ---------------------------------------------------------------------------
# Occupancy Offset Arrays (30-day deterministic patterns)
# ---------------------------------------------------------------------------

# Per-property occupancy offsets for each of the 30 days of generated data.
# These create realistic day-of-week and trend patterns without randomness.
# Values are percentage-point offsets from the base occupancy for that day type.

# Chicago: Business weekday peak - strong Mon-Thu, softer Fri-Sun
OCCUPANCY_OFFSETS_CHI: List[int] = [
    -2, 0, 1, 3, 2, -3, -5,   # Week 1 (Mon-Sun)
    -1, 1, 2, 4, 3, -2, -4,   # Week 2
    0, 2, 3, 5, 4, -1, -3,    # Week 3
    1, 3, 2, 4, 3, -2, -4,    # Week 4
    0, 2,                       # Partial week 5
]

# Miami: Leisure weekend peak - softer Mon-Thu, strong Fri-Sun
OCCUPANCY_OFFSETS_MIA: List[int] = [
    -4, -3, -2, -1, 2, 5, 4,   # Week 1
    -3, -2, -1, 0, 3, 6, 5,    # Week 2
    -2, -1, 0, 1, 4, 7, 6,     # Week 3
    -3, -2, -1, 0, 3, 5, 4,    # Week 4
    -2, -1,                      # Partial week 5
]

# Tokyo: Consistently high, low variance across all days
OCCUPANCY_OFFSETS_TYO: List[int] = [
    -1, 0, 1, 2, 1, 0, -1,    # Week 1
    0, 1, 2, 3, 2, 1, 0,      # Week 2
    1, 2, 3, 4, 3, 2, 1,      # Week 3
    0, 1, 2, 3, 2, 1, 0,      # Week 4
    1, 2,                       # Partial week 5
]

# Madrid: Mid-week dip pattern - Wednesday lowest, weekend recovery
OCCUPANCY_OFFSETS_MAD: List[int] = [
    -1, -3, -5, -2, 1, 4, 3,   # Week 1
    0, -2, -4, -1, 2, 5, 4,    # Week 2
    1, -1, -3, 0, 3, 6, 5,     # Week 3
    0, -2, -4, -1, 2, 5, 4,    # Week 4
    1, 0,                        # Partial week 5
]

# Mumbai: Growing trend - steady upward slope over 30 days
OCCUPANCY_OFFSETS_BOM: List[int] = [
    -4, -3, -3, -2, -2, -3, -4,  # Week 1 (low start)
    -2, -1, -1, 0, 0, -1, -2,    # Week 2
    0, 1, 1, 2, 2, 1, 0,         # Week 3
    2, 3, 3, 4, 4, 3, 2,         # Week 4
    4, 5,                          # Partial week 5 (highest)
]

# Lookup for revenue_generator: propertyId -> offset array
OCCUPANCY_OFFSETS: Dict[str, List[int]] = {
    "ALOHA-CHI-001": OCCUPANCY_OFFSETS_CHI,
    "ALOHA-MIA-001": OCCUPANCY_OFFSETS_MIA,
    "ALOHA-TYO-001": OCCUPANCY_OFFSETS_TYO,
    "ALOHA-MAD-001": OCCUPANCY_OFFSETS_MAD,
    "ALOHA-BOM-001": OCCUPANCY_OFFSETS_BOM,
}


# ---------------------------------------------------------------------------
# ADR Offset Arrays (30-day deterministic patterns)
# ---------------------------------------------------------------------------

# ADR offsets in currency units - ADR responds to demand (higher on peak days).
# Correlated with occupancy offsets: higher occupancy = higher ADR.

ADR_OFFSETS_CHI: List[int] = [
    -5, 0, 3, 8, 5, -8, -12,    # Week 1
    -3, 2, 5, 10, 8, -5, -10,   # Week 2
    0, 5, 8, 12, 10, -3, -8,    # Week 3
    2, 8, 5, 10, 8, -5, -10,    # Week 4
    0, 5,                         # Partial week 5
]

ADR_OFFSETS_MIA: List[int] = [
    -10, -8, -5, -3, 5, 15, 12,   # Week 1
    -8, -5, -3, 0, 8, 18, 15,     # Week 2
    -5, -3, 0, 3, 10, 20, 18,     # Week 3
    -8, -5, -3, 0, 8, 15, 12,     # Week 4
    -5, -3,                         # Partial week 5
]

ADR_OFFSETS_TYO: List[int] = [
    -3, 0, 2, 5, 3, 0, -2,    # Week 1
    0, 2, 5, 8, 5, 2, 0,      # Week 2
    2, 5, 8, 10, 8, 5, 2,     # Week 3
    0, 3, 5, 8, 5, 3, 0,      # Week 4
    2, 5,                       # Partial week 5
]

ADR_OFFSETS_MAD: List[int] = [
    -3, -8, -12, -5, 3, 10, 8,   # Week 1
    0, -5, -10, -3, 5, 12, 10,   # Week 2
    3, -3, -8, 0, 8, 15, 12,     # Week 3
    0, -5, -10, -3, 5, 12, 10,   # Week 4
    3, 0,                          # Partial week 5
]

ADR_OFFSETS_BOM: List[int] = [
    -8, -5, -5, -3, -3, -5, -8,   # Week 1
    -3, -2, 0, 2, 2, 0, -3,       # Week 2
    0, 3, 3, 5, 5, 3, 0,          # Week 3
    5, 8, 8, 10, 10, 8, 5,        # Week 4
    8, 10,                          # Partial week 5
]

# Lookup for revenue_generator: propertyId -> ADR offset array
ADR_OFFSETS: Dict[str, List[int]] = {
    "ALOHA-CHI-001": ADR_OFFSETS_CHI,
    "ALOHA-MIA-001": ADR_OFFSETS_MIA,
    "ALOHA-TYO-001": ADR_OFFSETS_TYO,
    "ALOHA-MAD-001": ADR_OFFSETS_MAD,
    "ALOHA-BOM-001": ADR_OFFSETS_BOM,
}


# ---------------------------------------------------------------------------
# Segment Mix Profiles (per property)
# ---------------------------------------------------------------------------

# Revenue segment breakdown per property reflecting their guest demographics.
# Values are percentage breakdowns that sum to 100.
SEGMENT_MIX: Dict[str, Dict[str, int]] = {
    # Chicago: Corporate-heavy business hotel
    "ALOHA-CHI-001": {
        "direct": 25,
        "ota": 15,
        "corporate": 35,
        "group": 15,
        "loyalty": 10,
    },
    # Miami: Leisure-heavy resort with strong OTA presence
    "ALOHA-MIA-001": {
        "direct": 30,
        "ota": 30,
        "corporate": 10,
        "group": 15,
        "loyalty": 15,
    },
    # Tokyo: Business-heavy with strong loyalty program
    "ALOHA-TYO-001": {
        "direct": 20,
        "ota": 15,
        "corporate": 30,
        "group": 20,
        "loyalty": 15,
    },
    # Madrid: Balanced leisure/business with strong direct
    "ALOHA-MAD-001": {
        "direct": 35,
        "ota": 25,
        "corporate": 15,
        "group": 10,
        "loyalty": 15,
    },
    # Mumbai: Growing market with strong corporate base
    "ALOHA-BOM-001": {
        "direct": 20,
        "ota": 20,
        "corporate": 30,
        "group": 20,
        "loyalty": 10,
    },
}


# ---------------------------------------------------------------------------
# Work Order Notes Templates
# ---------------------------------------------------------------------------

# Description templates for work order notes, indexed by issue type.
# The work_orders_generator selects and fills these deterministically.
WORK_ORDER_NOTES: Dict[str, List[str]] = {
    "HVAC": [
        "AC unit not cooling - guest complaint, thermostat reads 78F",
        "Heating system making intermittent noise - maintenance requested",
        "Air handler producing musty odor - filter replacement needed",
    ],
    "PLUMBING": [
        "Bathroom faucet leaking - drip rate increasing",
        "Toilet running continuously - flapper valve suspected",
        "Shower drain slow - possible hair clog",
    ],
    "ELECTRICAL": [
        "Outlet not providing power - circuit breaker not tripped",
        "Ceiling light fixture flickering - ballast may need replacement",
        "Room card reader intermittent - door lock mechanism checked",
    ],
    "HOUSEKEEPING": [
        "Deep clean required - previous guest extended stay (14 nights)",
        "Carpet stain reported - professional extraction needed",
        "Mattress replacement scheduled - exceeded rotation cycle",
    ],
    "STRUCTURAL": [
        "Window seal deteriorating - condensation between panes",
        "Bathroom tile grout cracking - water intrusion risk",
        "Door frame alignment off - difficulty closing",
    ],
    "IT_NETWORK": [
        "WiFi dead zone reported - signal strength below threshold",
        "Smart TV not connecting to streaming services",
        "In-room tablet unresponsive - firmware update required",
    ],
    "ELEVATOR": [
        "Elevator car leveling issue - 2cm gap at floor 12",
        "Door sensor delay causing slow operation - calibration needed",
        "Emergency phone line static - communication test failed",
    ],
}


# ---------------------------------------------------------------------------
# TTL Configuration (in days)
# ---------------------------------------------------------------------------

# Time-to-live settings for each table. Items older than TTL_DAYS from their
# creation date will be automatically deleted by DynamoDB.
TTL_RESERVATIONS_DAYS: int = 60
TTL_WORK_ORDERS_DAYS: int = 60
TTL_REVENUES_DAYS: int = 90

# ---------------------------------------------------------------------------
# Batch Write Configuration
# ---------------------------------------------------------------------------

# DynamoDB BatchWriteItem limit per request
BATCH_WRITE_SIZE: int = 25

# Exponential backoff configuration for UnprocessedItems retries
MAX_RETRIES: int = 8
BACKOFF_BASE_MS: int = 50
BACKOFF_MAX_MS: int = 5000
