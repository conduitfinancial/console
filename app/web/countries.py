"""ISO 3166-1 alpha-3 → English short name, **display layer only**.

This table is never used to validate, gate, or enumerate what Conduit supports:
discovery (`GET /v2/onboarding/requirements`, the payout/transfer requirements
responses) remains the only authority on which countries exist, which are
allowed, and which are blocked. Nothing here filters a select, refuses an input,
or decides a route — a code absent from this table simply renders without a
name, and a code Conduit stops accepting keeps its name until it stops arriving.

It exists because an operator reading `BGR` in a 248-row select is reading a
machine's answer, not a human's. Adding the name back is a *display* affordance,
and it is a display-scoped exception to the repo's "no hardcoded country data"
rule.

Names are the common English short forms an operator recognises, not formal
state names ("South Korea", not "Republic of Korea").
"""

from __future__ import annotations

import re

from app.accounts import humanize

# Roughly the ISO listing's own order; the common names carried by the rows
# (see the module docstring) put ten of them out of alphabetical sequence, so
# ordering is *not* a review invariant and must not be read as one. What guards
# the pairings is tests/test_countries.py.
COUNTRIES: dict[str, str] = {
    "AFG": "Afghanistan",
    "ALA": "Åland Islands",
    "ALB": "Albania",
    "DZA": "Algeria",
    "ASM": "American Samoa",
    "AND": "Andorra",
    "AGO": "Angola",
    "AIA": "Anguilla",
    "ATA": "Antarctica",
    "ATG": "Antigua and Barbuda",
    "ARG": "Argentina",
    "ARM": "Armenia",
    "ABW": "Aruba",
    "AUS": "Australia",
    "AUT": "Austria",
    "AZE": "Azerbaijan",
    "BHS": "Bahamas",
    "BHR": "Bahrain",
    "BGD": "Bangladesh",
    "BRB": "Barbados",
    "BLR": "Belarus",
    "BEL": "Belgium",
    "BLZ": "Belize",
    "BEN": "Benin",
    "BMU": "Bermuda",
    "BTN": "Bhutan",
    "BOL": "Bolivia",
    "BES": "Bonaire, Sint Eustatius and Saba",
    "BIH": "Bosnia and Herzegovina",
    "BWA": "Botswana",
    "BVT": "Bouvet Island",
    "BRA": "Brazil",
    "IOT": "British Indian Ocean Territory",
    "BRN": "Brunei",
    "BGR": "Bulgaria",
    "BFA": "Burkina Faso",
    "BDI": "Burundi",
    "CPV": "Cape Verde",
    "KHM": "Cambodia",
    "CMR": "Cameroon",
    "CAN": "Canada",
    "CYM": "Cayman Islands",
    "CAF": "Central African Republic",
    "TCD": "Chad",
    "CHL": "Chile",
    "CHN": "China",
    "CXR": "Christmas Island",
    "CCK": "Cocos (Keeling) Islands",
    "COL": "Colombia",
    "COM": "Comoros",
    "COG": "Republic of the Congo",
    "COD": "Democratic Republic of the Congo",
    "COK": "Cook Islands",
    "CRI": "Costa Rica",
    "CIV": "Côte d'Ivoire",
    "HRV": "Croatia",
    "CUB": "Cuba",
    "CUW": "Curaçao",
    "CYP": "Cyprus",
    "CZE": "Czechia",
    "DNK": "Denmark",
    "DJI": "Djibouti",
    "DMA": "Dominica",
    "DOM": "Dominican Republic",
    "ECU": "Ecuador",
    "EGY": "Egypt",
    "SLV": "El Salvador",
    "GNQ": "Equatorial Guinea",
    "ERI": "Eritrea",
    "EST": "Estonia",
    "SWZ": "Eswatini",
    "ETH": "Ethiopia",
    "FLK": "Falkland Islands",
    "FRO": "Faroe Islands",
    "FJI": "Fiji",
    "FIN": "Finland",
    "FRA": "France",
    "GUF": "French Guiana",
    "PYF": "French Polynesia",
    "ATF": "French Southern Territories",
    "GAB": "Gabon",
    "GMB": "Gambia",
    "GEO": "Georgia",
    "DEU": "Germany",
    "GHA": "Ghana",
    "GIB": "Gibraltar",
    "GRC": "Greece",
    "GRL": "Greenland",
    "GRD": "Grenada",
    "GLP": "Guadeloupe",
    "GUM": "Guam",
    "GTM": "Guatemala",
    "GGY": "Guernsey",
    "GIN": "Guinea",
    "GNB": "Guinea-Bissau",
    "GUY": "Guyana",
    "HTI": "Haiti",
    "HMD": "Heard Island and McDonald Islands",
    "VAT": "Vatican City",
    "HND": "Honduras",
    "HKG": "Hong Kong",
    "HUN": "Hungary",
    "ISL": "Iceland",
    "IND": "India",
    "IDN": "Indonesia",
    "IRN": "Iran",
    "IRQ": "Iraq",
    "IRL": "Ireland",
    "IMN": "Isle of Man",
    "ISR": "Israel",
    "ITA": "Italy",
    "JAM": "Jamaica",
    "JPN": "Japan",
    "JEY": "Jersey",
    "JOR": "Jordan",
    "KAZ": "Kazakhstan",
    "KEN": "Kenya",
    "KIR": "Kiribati",
    "PRK": "North Korea",
    "KOR": "South Korea",
    "KWT": "Kuwait",
    "KGZ": "Kyrgyzstan",
    "LAO": "Laos",
    "LVA": "Latvia",
    "LBN": "Lebanon",
    "LSO": "Lesotho",
    "LBR": "Liberia",
    "LBY": "Libya",
    "LIE": "Liechtenstein",
    "LTU": "Lithuania",
    "LUX": "Luxembourg",
    "MAC": "Macao",
    "MDG": "Madagascar",
    "MWI": "Malawi",
    "MYS": "Malaysia",
    "MDV": "Maldives",
    "MLI": "Mali",
    "MLT": "Malta",
    "MHL": "Marshall Islands",
    "MTQ": "Martinique",
    "MRT": "Mauritania",
    "MUS": "Mauritius",
    "MYT": "Mayotte",
    "MEX": "Mexico",
    "FSM": "Micronesia",
    "MDA": "Moldova",
    "MCO": "Monaco",
    "MNG": "Mongolia",
    "MNE": "Montenegro",
    "MSR": "Montserrat",
    "MAR": "Morocco",
    "MOZ": "Mozambique",
    "MMR": "Myanmar",
    "NAM": "Namibia",
    "NRU": "Nauru",
    "NPL": "Nepal",
    "NLD": "Netherlands",
    "NCL": "New Caledonia",
    "NZL": "New Zealand",
    "NIC": "Nicaragua",
    "NER": "Niger",
    "NGA": "Nigeria",
    "NIU": "Niue",
    "NFK": "Norfolk Island",
    "MKD": "North Macedonia",
    "MNP": "Northern Mariana Islands",
    "NOR": "Norway",
    "OMN": "Oman",
    "PAK": "Pakistan",
    "PLW": "Palau",
    "PSE": "Palestine",
    "PAN": "Panama",
    "PNG": "Papua New Guinea",
    "PRY": "Paraguay",
    "PER": "Peru",
    "PHL": "Philippines",
    "PCN": "Pitcairn Islands",
    "POL": "Poland",
    "PRT": "Portugal",
    "PRI": "Puerto Rico",
    "QAT": "Qatar",
    "REU": "Réunion",
    "ROU": "Romania",
    "RUS": "Russia",
    "RWA": "Rwanda",
    "BLM": "Saint Barthélemy",
    "SHN": "Saint Helena",
    "KNA": "Saint Kitts and Nevis",
    "LCA": "Saint Lucia",
    "MAF": "Saint Martin",
    "SPM": "Saint Pierre and Miquelon",
    "VCT": "Saint Vincent and the Grenadines",
    "WSM": "Samoa",
    "SMR": "San Marino",
    "STP": "São Tomé and Príncipe",
    "SAU": "Saudi Arabia",
    "SEN": "Senegal",
    "SRB": "Serbia",
    "SYC": "Seychelles",
    "SLE": "Sierra Leone",
    "SGP": "Singapore",
    "SXM": "Sint Maarten",
    "SVK": "Slovakia",
    "SVN": "Slovenia",
    "SLB": "Solomon Islands",
    "SOM": "Somalia",
    "ZAF": "South Africa",
    "SGS": "South Georgia and the South Sandwich Islands",
    "SSD": "South Sudan",
    "ESP": "Spain",
    "LKA": "Sri Lanka",
    "SDN": "Sudan",
    "SUR": "Suriname",
    "SJM": "Svalbard and Jan Mayen",
    "SWE": "Sweden",
    "CHE": "Switzerland",
    "SYR": "Syria",
    "TWN": "Taiwan",
    "TJK": "Tajikistan",
    "TZA": "Tanzania",
    "THA": "Thailand",
    "TLS": "Timor-Leste",
    "TGO": "Togo",
    "TKL": "Tokelau",
    "TON": "Tonga",
    "TTO": "Trinidad and Tobago",
    "TUN": "Tunisia",
    "TUR": "Turkey",
    "TKM": "Turkmenistan",
    "TCA": "Turks and Caicos Islands",
    "TUV": "Tuvalu",
    "UGA": "Uganda",
    "UKR": "Ukraine",
    "ARE": "United Arab Emirates",
    "GBR": "United Kingdom",
    "USA": "United States",
    "UMI": "United States Minor Outlying Islands",
    "URY": "Uruguay",
    "UZB": "Uzbekistan",
    "VUT": "Vanuatu",
    "VEN": "Venezuela",
    "VNM": "Vietnam",
    "VGB": "British Virgin Islands",
    "VIR": "U.S. Virgin Islands",
    "WLF": "Wallis and Futuna",
    "ESH": "Western Sahara",
    "YEM": "Yemen",
    "ZMB": "Zambia",
    "ZWE": "Zimbabwe",
}

# Conservative on purpose: an all-caps token with an underscore is a machine
# constant nobody writes by hand, so prettifying it cannot be mistaken for
# prettifying a human string discovery meant literally.
_ENUM_CONSTANT = re.compile(r"^[A-Z0-9]+(?:_[A-Z0-9]+)+$")


def country_name(code: str | None) -> str | None:
    """The English short name for an alpha-3 code, or None. Case-insensitive."""
    return COUNTRIES.get(code.strip().upper()) if isinstance(code, str) else None


# The table read backwards, for `resolve_country`. Built once; the forward table
# is the only source, so the two can never disagree about a pairing.
_BY_NAME: dict[str, str] = {name.casefold(): code for code, name in COUNTRIES.items()}


def resolve_country(value: str | None) -> str:
    """"United States" → `USA`. An operator's typed country name, turned into the
    alpha-3 code the API takes.

    **Exact, full-name, case-insensitive matches only**, against this module's
    own display table. Everything else — a code, a partial name, a misspelling,
    a country this table has never heard of, a country Conduit does not serve —
    passes through byte-identical, so what the operator sees is Conduit's own
    problem-detail rather than this console deciding in advance what exists.
    There is deliberately no fuzzy matching: a near-miss resolved to the wrong
    jurisdiction is a payment sent to the wrong country, and the cost of the
    honest miss is one retype.

    Display-layer in the same sense as the rest of this module: it never
    validates, never refuses, and never enumerates what is allowed.
    """
    if not isinstance(value, str):
        return ""
    return _BY_NAME.get(value.strip().casefold(), value)


def humanize_enum(value: str | None) -> str | None:
    """`INTERCOMPANY_TRANSFER` → `Intercompany transfer`; None for anything else."""
    if isinstance(value, str) and _ENUM_CONSTANT.match(value):
        return value.replace("_", " ").capitalize()
    return None


def enum_label(value: str) -> str:
    """`deposit_return` → "Deposit return": operator language for a raw API enum.

    QA F-002 — purposes, transaction kinds, subject types and application types
    all reach the screen as the wire's own lowercase constants. This says them in
    words; it never *replaces* the value, which stays beside it wherever it is
    rendered (and is always what the form or the link submits).

    `accounts.humanize` is the transformation, not a second copy of it: it
    already turns `sort_code` and `sortCode` into "Sort code" for the deposit
    tables, which is exactly this job.
    """
    return humanize(value) if value else ""


def enum_option(value: str) -> str:
    """The same thing for an `<option>`, which can hold text and no markup:
    label first, the wire value after it — the reading order the chips and tabs
    use, with the two halves in one string because an option has nowhere else to
    put the second one.

    The raw half is dropped when it differs from the label by capitalisation
    alone (`application` → "Application"), where it would only repeat the label
    back at the operator.
    """
    label = enum_label(value)
    return label if label.lower() == value.lower() else f"{label} — {value}"


def option_label(value: str, label: str) -> str:
    """What a `<select>` option says, given discovery's own label.

    Discovery's label wins whenever there is one (`forms._options` falls back to
    the raw value when `options[]` carries none, so `label == value` is exactly
    the no-label case). Only then does this add a name, and it *appends* — the
    raw value stays on screen, because the value is what Conduit's errors, its
    API and the operator's other tab all speak.
    """
    if label != value:
        return label
    name = country_name(value) or humanize_enum(value)
    return f"{value} — {name}" if name else label
