"""Seed data and the deterministic rules everything else keys off (DESIGN.md §2).

Behaviour is a pure function of the member number so every evidence run is
reproducible:

    10000-49999 -> Eastern region  (accessible to the operator)
    50000-89999 -> Western region  (profile access denied -> PERMISSION_DENIED)
    anything else / unseeded -> "No member matches this number"

All member data is fabricated. No real PII, ever.
"""

import re

# The logged-in operator is hardcoded; there is no user management in the prop.
OPERATOR_NAME = "E. Okafor"
OPERATOR_REGION = "Eastern"

MEMBER_NUMBER_RE = re.compile(r"^[0-9]{5}$")

EASTERN_RANGE = (10000, 49999)
WESTERN_RANGE = (50000, 89999)

# id, name, age, region, savings_balance, loan_taken, loan_amount, credit_score
MEMBERS = {
    "12345": {
        "id": "12345", "name": "Alice Torres", "age": 34, "region": "Eastern",
        "savings_balance": 4523.18, "loan_taken": True, "loan_amount": 15000.00,
        "credit_score": 712,
    },
    "23456": {
        "id": "23456", "name": "Marcus Bell", "age": 41, "region": "Eastern",
        "savings_balance": 18240.55, "loan_taken": False, "loan_amount": 0.00,
        "credit_score": 688,
    },
    "10042": {
        "id": "10042", "name": "Priya Raman", "age": 29, "region": "Eastern",
        "savings_balance": 902.40, "loan_taken": True, "loan_amount": 6200.00,
        "credit_score": 640,
    },
    "31877": {
        "id": "31877", "name": "Daniel Okonjo", "age": 57, "region": "Eastern",
        "savings_balance": 76310.02, "loan_taken": False, "loan_amount": 0.00,
        "credit_score": 795,
    },
    "44120": {
        "id": "44120", "name": "Yuki Tanaka", "age": 38, "region": "Eastern",
        "savings_balance": 12055.75, "loan_taken": True, "loan_amount": 24500.00,
        "credit_score": 731,
    },
    "28903": {
        "id": "28903", "name": "Rosa Delgado", "age": 46, "region": "Eastern",
        "savings_balance": 3310.09, "loan_taken": False, "loan_amount": 0.00,
        "credit_score": 668,
    },
    "67890": {
        "id": "67890", "name": "Evelyn Cross", "age": 33, "region": "Western",
        "savings_balance": 8720.00, "loan_taken": True, "loan_amount": 9800.00,
        "credit_score": 702,
    },
    "55014": {
        "id": "55014", "name": "Tomas Nilsen", "age": 61, "region": "Western",
        "savings_balance": 45210.90, "loan_taken": False, "loan_amount": 0.00,
        "credit_score": 754,
    },
    "71226": {
        "id": "71226", "name": "Grace Mbeki", "age": 27, "region": "Western",
        "savings_balance": 1580.33, "loan_taken": True, "loan_amount": 3100.00,
        "credit_score": 615,
    },
    "88301": {
        "id": "88301", "name": "Henry Fowler", "age": 50, "region": "Western",
        "savings_balance": 29405.66, "loan_taken": False, "loan_amount": 0.00,
        "credit_score": 723,
    },
}

ACCOUNT_TYPES = [
    "Regular Savings",
    "Holiday Club",
    "Youth Savings",
    "Money Market",
]

FUNDING_SOURCES = [
    "Transfer from primary savings",
    "External ACH transfer",
    "Cash at branch",
]


def is_valid_member_number(raw):
    """A member number is exactly five digits. Anything else is malformed."""
    return bool(MEMBER_NUMBER_RE.match((raw or "").strip()))


def region_for_id(raw):
    """Region implied by the number range, independent of whether the member exists."""
    if not is_valid_member_number(raw):
        return None
    n = int(raw)
    if EASTERN_RANGE[0] <= n <= EASTERN_RANGE[1]:
        return "Eastern"
    if WESTERN_RANGE[0] <= n <= WESTERN_RANGE[1]:
        return "Western"
    return None


def get_member(raw):
    """Seeded member record, or None -> the caller renders MEMBER_NOT_FOUND."""
    if not is_valid_member_number(raw):
        return None
    return MEMBERS.get(raw.strip())


def in_operator_region(member):
    return member["region"] == OPERATOR_REGION


def money(value):
    """2003-era currency rendering: $4,523.18"""
    return "${:,.2f}".format(value)


def sub_account_number(member_id, account_type):
    """Deterministic so the mutation flow produces reproducible evidence."""
    seq = sum(ord(c) for c in account_type) % 97
    return "SA-{}-{:02d}".format(member_id, seq)
