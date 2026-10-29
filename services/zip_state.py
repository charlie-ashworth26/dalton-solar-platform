"""
ZIP code -> US state, by ZIP3 prefix.

WHY THIS EXISTS
    The Availability screen collects email, ZIP and utility. It has no state
    field, and the market-rules utility endpoint is keyed BY STATE
    (GET /markets/utilities?state=XX). So something has to turn the ZIP the rep
    typed into a state before the utility list can be fetched.

WHAT THIS IS NOT
    This is NOT ZIP-to-utility mapping, which stays deferred. It answers
    exactly one question - which state is this ZIP in - and nothing else. Which
    utilities are available in that state remains Perch's answer, fetched at
    runtime from the market-rules endpoints. A ZIP never selects a utility here.

WHY ZIP3 RANGES RATHER THAN A FULL ZIP DATABASE
    US ZIP codes are allocated geographically and the first three digits
    (the "ZIP3", or sectional centre facility) fall within one state. A full
    5-digit dataset is ~42,000 rows that would need maintaining; the ZIP3
    allocation is ~900 prefixes expressible as the ~70 contiguous ranges below,
    is stable over years, needs no network call and no migration.

    The table covers ALL states deliberately, not only the markets UBS serves.
    A New Jersey ZIP must resolve to NJ - and then return no supported
    utilities with a clear message - rather than being mistaken for a
    neighbouring state UBS does serve. Partial coverage would turn an
    out-of-market ZIP into a wrong-market enrollment.

PRECISION
    ZIP3 is accurate at state level for ordinary geographic ZIPs. A handful of
    ZIP3s genuinely straddle a state line for mail-routing reasons; where that
    happens the dominant state is used. This is a convenience that preselects a
    dropdown, never an authority: the rep can always change the utility, Perch
    validates the ZIP against the utility's service area at capacity time, and
    a wrong guess surfaces there rather than being silently enrolled.
"""

# (low_prefix, high_prefix, state) - inclusive, on the first three digits.
_ZIP3_RANGES = (
    (4, 5, "NY"),      # Holtsville NY (004-005)
    (6, 9, "PR"),      # Puerto Rico / Virgin Islands
    (10, 27, "MA"), (28, 29, "RI"), (30, 38, "NH"), (39, 49, "ME"),
    (50, 59, "VT"), (60, 69, "CT"), (70, 89, "NJ"),
    (100, 149, "NY"),
    (150, 196, "PA"), (197, 199, "DE"),
    (200, 200, "DC"), (201, 201, "VA"), (202, 205, "DC"),
    (206, 219, "MD"),
    (220, 246, "VA"),
    (247, 268, "WV"), (270, 289, "NC"), (290, 299, "SC"),
    (300, 319, "GA"), (320, 349, "FL"), (350, 369, "AL"),
    (370, 385, "TN"), (386, 397, "MS"), (398, 399, "GA"),
    (400, 427, "KY"), (430, 459, "OH"), (460, 479, "IN"), (480, 499, "MI"),
    (500, 528, "IA"), (530, 549, "WI"), (550, 567, "MN"),
    (570, 577, "SD"), (580, 588, "ND"), (590, 599, "MT"),
    (600, 629, "IL"), (630, 658, "MO"), (660, 679, "KS"), (680, 693, "NE"),
    (700, 714, "LA"), (716, 729, "AR"), (730, 749, "OK"), (750, 799, "TX"),
    (800, 816, "CO"), (820, 831, "WY"), (832, 838, "ID"), (840, 847, "UT"),
    (850, 860, "AZ"), (863, 865, "AZ"), (870, 884, "NM"), (889, 898, "NV"),
    (900, 961, "CA"), (967, 968, "HI"), (970, 979, "OR"), (980, 994, "WA"),
    (995, 999, "AK"),
)


def state_for_zip(zip_code):
    """Return the two-letter state for a 5-digit ZIP, or None.

    None means "could not determine", which callers must treat as "ask the rep"
    or "no supported utilities" - never as a default state. Returning a guess
    would be worse than returning nothing.
    """
    z = (zip_code or "").strip()
    if len(z) != 5 or not z.isdigit():
        return None
    prefix = int(z[:3])
    for low, high, state in _ZIP3_RANGES:
        if low <= prefix <= high:
            return state
    return None


def covered_states():
    """Every state this table can resolve. Diagnostics and tests only - it says
    nothing about which markets Perch serves."""
    return sorted({state for _, _, state in _ZIP3_RANGES})
