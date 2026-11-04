"""Conservative US-location evidence shared by both job lanes.

Location text is noisy provider metadata.  This module deliberately refuses to
infer an employer headquarters or treat a bare ``Remote``/state abbreviation
as US authorization.  Unknown rows remain discoverable, but callers can keep
them out of automatic alerts.
"""
from __future__ import annotations

import re
from functools import lru_cache

_US_COUNTRY_RE = re.compile(r"\b(?:united states|u\.s\.a?\.?|usa|us)\b", re.I)
_REMOTE_US_RE = re.compile(r"\bremote\s*(?:[-–,()]\s*)?(?:in\s+)?(?:the\s+)?(?:united states|u\.s\.a?\.?|usa)\b", re.I)
_REMOTE_RE = re.compile(r"\b(?:remote|telecommute|work\s+from\s+anywhere)\b", re.I)
_NONLOCATION_US_RE = re.compile(
    r"\b(?:u\.s\.a?\.?|usa|us)\s*(?:[-–]\s*)?"
    r"(?:shift|hours?|time(?:\s+zone)?|clients?|team)\b", re.I)
_NEGATIVE_US_RE = re.compile(
    r"\b(?:excluding|except|outside|not\s+in|not\s+eligible\s+in|anywhere\s+but)\b[^.;|]*"
    r"\b(?:united states|u\.s\.a?\.?|usa|us)\b", re.I)
_FOREIGN_COUNTRY_RE = re.compile(
    # This is intentionally a country list rather than a city list.  City
    # names are maintained separately below because several collide with US
    # cities/states (for example Paris, TX and Dublin, CA).  Georgia is also
    # omitted here: a bare value is ambiguous, while a known foreign city
    # such as Tbilisi is handled by _FOREIGN_CITY_RE.
    r"\b(?:afghanistan|albania|algeria|andorra|angola|antigua and barbuda|"
    r"argentina|armenia|australia|austria|azerbaijan|bahamas|bahrain|bangladesh|"
    r"barbados|belarus|belgium|belize|benin|bhutan|bolivia|bosnia and herzegovina|"
    r"botswana|brazil|brunei|bulgaria|burkina faso|burundi|cabo verde|cambodia|"
    r"cameroon|canada|central african republic|chad|chile|china|colombia|comoros|"
    r"congo|costa rica|croatia|cuba|cyprus|czechia|czech republic|denmark|djibouti|"
    r"dominica|dominican republic|east timor|ecuador|egypt|el salvador|"
    r"equatorial guinea|eritrea|estonia|eswatini|ethiopia|fiji|finland|france|"
    r"gabon|gambia|germany|ghana|greece|grenada|guatemala|guinea|guyana|haiti|"
    r"honduras|hungary|iceland|india|indonesia|iran|iraq|ireland|israel|italy|"
    r"ivory coast|jamaica|japan|jordan|kazakhstan|kenya|kiribati|kuwait|kyrgyzstan|"
    r"laos|latvia|lebanon|lesotho|liberia|libya|liechtenstein|lithuania|luxembourg|"
    r"madagascar|malawi|malaysia|maldives|mali|malta|marshall islands|mauritania|"
    # Keep Mexico out when it is the US state name "New Mexico".  The
    # negative look-behind is fixed-width so the compiled expression remains
    # portable across the supported Python versions.
    r"mauritius|(?<!new )mexico|micronesia|moldova|monaco|mongolia|montenegro|morocco|"
    r"mozambique|myanmar|namibia|nauru|nepal|netherlands|new zealand|nicaragua|"
    r"niger|nigeria|north korea|north macedonia|norway|oman|pakistan|palau|panama|"
    r"papua new guinea|paraguay|peru|philippines|poland|portugal|qatar|romania|"
    r"russia|rwanda|saudi arabia|senegal|serbia|seychelles|sierra leone|singapore|"
    r"slovakia|slovenia|solomon islands|somalia|south africa|south korea|south sudan|"
    r"spain|sri lanka|sudan|suriname|sweden|switzerland|syria|taiwan|tajikistan|"
    r"tanzania|thailand|timor-leste|togo|tonga|trinidad and tobago|tunisia|"
    r"turkey|türkiye|turkmenistan|tuvalu|uganda|ukraine|united arab emirates|"
    r"united kingdom|\buk\b|uruguay|uzbekistan|vanuatu|venezuela|vietnam|yemen|"
    r"zambia|zimbabwe|hong kong)\b", re.I)
_FOREIGN_REGION_RE = re.compile(
    r"\b(?:europe|emea|apac|asia|eu|european union|middle east|africa|oceania|"
    r"latin america|south america|worldwide outside the us)\b", re.I)
_FOREIGN_CITY_RE = re.compile(
    r"\b(?:tbilisi|belgrade|aarhus|doha|toronto|vancouver|montreal|london|"
    r"berlin|munich|amsterdam|bangkok|kuala lumpur|stevenage|wellington|rome|"
    r"seoul|ottawa|gratkorn|bengaluru|bangalore)\b", re.I)
_US_METRO_RE = re.compile(
    r"\b(?:nyc|new york city|new york|sf|san francisco|boston|dallas|san jose)\b", re.I)
_STATE_NAMES = (
    "alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|"
    "georgia|hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|"
    "massachusetts|michigan|minnesota|mississippi|missouri|montana|nebraska|nevada|"
    "new hampshire|new jersey|new mexico|new york|north carolina|north dakota|ohio|oklahoma|"
    "oregon|pennsylvania|rhode island|south carolina|south dakota|tennessee|texas|utah|"
    "vermont|virginia|washington|west virginia|wisconsin|wyoming|district of columbia"
)
_US_STATE_NAME_RE = re.compile(rf"\b(?:{_STATE_NAMES})\b", re.I)
_US_STATE_CODES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN",
    "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV",
    "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN",
    "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC",
}
_REMOTE_ONLY_PREFIX_RE = re.compile(
    r"^(?:remote|telecommute|work\s+from\s+anywhere|anywhere)(?:\s+in)?$", re.I)
_WORK_ARRANGEMENT_LABEL_RE = re.compile(
    r"^(?:in[- ]?(?:office|person)|hybrid)$", re.I)
_STATE_REMOTE_LABEL_RE = re.compile(r"^[A-Z]{2}[- ]remote$", re.I)


def _us_state_context(value: str) -> bool:
    """Accept a state code only next to a city/second location token.

    A bare ``CA`` or ``IN`` is intentionally unknown because those values are
    also common country/provider abbreviations.
    """
    upper = value.upper().strip()
    if upper in _US_STATE_CODES:
        return False
    # ATS feeds sometimes emit work-arrangement labels as location values.
    # ``IN-Office``/``IN-person`` must not be mistaken for Indiana's code;
    # these labels are unknown until an actual place is listed.
    if _WORK_ARRANGEMENT_LABEL_RE.fullmatch(value.strip()):
        return False
    if _STATE_REMOTE_LABEL_RE.fullmatch(value.strip()):
        return False
    # Full state names also require a neighboring city/region; bare Georgia
    # and bare Washington remain ambiguous.
    state_match = _US_STATE_NAME_RE.search(value)
    if state_match:
        prefix = value[:state_match.start()].strip(" ,/-")
        if _REMOTE_ONLY_PREFIX_RE.fullmatch(prefix):
            # "Remote, CA" and "Remote, Georgia" describe a remote scope,
            # not a verified US work location.  The country/state ambiguity is
            # especially important for Georgia.
            return False
        if prefix:
            return True
        # Most bare state names are safe US evidence. Georgia is deliberately
        # withheld because it is also a country; a bare value stays unknown.
        return state_match.group(0).casefold() != "georgia"
    # Require punctuation around two-letter codes. This avoids treating the
    # preposition ``in`` in ``Remote in EMEA`` as Indiana.
    for match in re.finditer(r"(?:[,/\-])\s*([A-Z]{2})(?=$|[\s,;/\-])", value.upper()):
        code = match.group(1)
        if code in _US_STATE_CODES:
            prefix = value[:match.start()].strip(" ,/-")
            suffix = value[match.end():].strip(" ,/-")
            if _REMOTE_ONLY_PREFIX_RE.fullmatch(prefix):
                return False
            if prefix or suffix:
                return True
    # A few ATS providers omit the comma (for example ``London OH``).  Only
    # accept that shape when it is clearly a location-like ``<place> <code>``
    # pair; the remote-only prefixes above remain deliberately unverified.
    compact = re.fullmatch(r"(.+?)\s+([A-Z]{2})", value.strip(), re.I)
    if compact and compact.group(2).upper() in _US_STATE_CODES:
        prefix = compact.group(1).strip(" ,/-")
        if prefix and not _REMOTE_ONLY_PREFIX_RE.fullmatch(prefix):
            return True
    if re.match(r"^[A-Z]{2}[-/]\s*[A-Z]", value.upper()):
        return value[:2].upper() in _US_STATE_CODES
    return False


def _state_code_context(value: str) -> str | None:
    """Return an explicit US state code attached to a location token."""
    for match in re.finditer(r"(?:[,/\-])\s*([A-Z]{2})(?=$|[\s,;/\-])", value.upper()):
        code = match.group(1)
        prefix = value[:match.start()].strip(" ,/-")
        if code in _US_STATE_CODES and prefix and not _REMOTE_ONLY_PREFIX_RE.fullmatch(prefix):
            return code
    compact = re.fullmatch(r"(.+?)\s+([A-Z]{2})", value.strip(), re.I)
    if compact and compact.group(2).upper() in _US_STATE_CODES:
        prefix = compact.group(1).strip(" ,/-")
        if prefix and not _REMOTE_ONLY_PREFIX_RE.fullmatch(prefix):
            return compact.group(2).upper()
    return None


def _foreign_city_has_explicit_us_state(value: str) -> bool:
    """Whether a foreign-looking city is explicitly paired with a US state.

    Several legitimate US towns share names with international cities.  A
    concrete state suffix disambiguates London/OH, Ottawa/IL, Rome/GA,
    Toronto/KS, and Vancouver/WA.  Toronto/CA stays conservative because CA
    is also the natural Canada abbreviation, and Bengaluru/IN is clearly the
    Indian city rather than Indiana.
    """
    city = _FOREIGN_CITY_RE.search(value)
    if not city:
        return False
    name = city.group(0).casefold()
    if name in {"bengaluru", "bangalore"}:
        return False
    code = _state_code_context(value)
    if code:
        if name == "toronto" and code == "CA":
            return False
        return True
    # Full state names are also explicit suffix evidence. Keep the known
    # Tbilisi/Georgia country collision conservative; unlike a US state suffix,
    # that exact pair is a strongly established foreign location.
    state = _US_STATE_NAME_RE.search(value)
    if state:
        prefix = value[:state.start()].strip(" ,/-")
        if prefix and not _REMOTE_ONLY_PREFIX_RE.fullmatch(prefix):
            return not (name == "tbilisi" and state.group(0).casefold() == "georgia")
    return False


def _split_options(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"\s*;\s*|\s*\|\s*|\s+·\s+", value) if part.strip()]


def _assess_location_tokens(tokens: tuple[str, ...], remote: bool) -> tuple[str, tuple[int, ...], str]:
    """Return an immutable classification for normalized location tokens."""
    us_hits: list[int] = []
    foreign_hits: list[int] = []
    for index, value in enumerate(tokens):
        # US shift/timezone/client/team wording describes schedule or
        # audience, not where the candidate may work. Mask only these bounded
        # phrases; explicit "United States" remains location evidence.
        location_value = _NONLOCATION_US_RE.sub(" ", value)
        if _NEGATIVE_US_RE.search(location_value):
            foreign_hits.append(index)
            continue
        # Explicit US country tokens win before city/state ambiguity.
        if _US_COUNTRY_RE.search(location_value) or _REMOTE_US_RE.search(location_value):
            us_hits.append(index)
            if (_FOREIGN_COUNTRY_RE.search(location_value)
                    or _FOREIGN_REGION_RE.search(location_value)
                    or (_FOREIGN_CITY_RE.search(location_value)
                        and not _foreign_city_has_explicit_us_state(location_value))):
                foreign_hits.append(index)
            continue
        state_context = _us_state_context(location_value)
        foreign_country = (_FOREIGN_COUNTRY_RE.search(location_value)
                           or _FOREIGN_REGION_RE.search(location_value))
        foreign_city = _FOREIGN_CITY_RE.search(location_value)
        # US state context wins for ambiguous cities such as Paris, TX,
        # London, OH, and Vancouver, WA. A few city/state pairs remain
        # conservative where the suffix is itself ambiguous (Toronto, CA) or
        # the city is unambiguously international (Bengaluru, IN).
        if foreign_country:
            foreign_hits.append(index)
        elif state_context and (not foreign_city
                                or _foreign_city_has_explicit_us_state(location_value)):
            us_hits.append(index)
        elif foreign_country or foreign_city:
            foreign_hits.append(index)
        elif _US_METRO_RE.search(value):
            us_hits.append(index)
    if us_hits and foreign_hits:
        return "mixed", tuple(dict.fromkeys(us_hits + foreign_hits))[:8], \
            "US and foreign location options are both listed"
    if us_hits:
        return "us", tuple(dict.fromkeys(us_hits))[:8], "confirmed US location evidence"
    if foreign_hits:
        return "non_us", tuple(dict.fromkeys(foreign_hits))[:8], \
            "only foreign location evidence is listed"
    if tokens and any(_REMOTE_RE.search(value) for value in tokens):
        return "unknown", (), "remote scope is not tied to a country"
    return "unknown", (), "location is missing or not specific enough to verify US work eligibility"


_assess_location_tokens_cached = lru_cache(maxsize=8192)(_assess_location_tokens)


def assess_us_location(locations: list[str], *, remote: bool = False) -> dict:
    """Classify posting locations using only bounded, explicit evidence.

    The classifier is called repeatedly during gate, score, and dedupe passes.
    Cache only immutable normalized tokens and evidence indexes; each public
    call reconstructs a fresh dictionary/list so callers cannot poison future
    results by mutating the returned evidence.
    """
    values = tuple(
        part for value in (locations or [])
        for part in _split_options(str(value).strip()) if part
    )
    normalized = tuple(value.casefold() for value in values)
    status, evidence_indexes, reason = _assess_location_tokens_cached(
        normalized, bool(remote))
    return {
        "status": status,
        "evidence": [values[index] for index in evidence_indexes],
        "reason": reason,
    }
