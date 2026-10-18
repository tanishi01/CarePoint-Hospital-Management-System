import re
import unicodedata


EMAIL_PATTERN = re.compile(
    r"^[A-Za-z0-9]+(?:[._+-][A-Za-z0-9]+)*"
    r"@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)
ROOM_NUMBER_PATTERN = re.compile(r'(?:[A-Za-z]+(?:[ -])?)?[0-9]+')
INDIAN_MOBILE_PATTERN = re.compile(r'^[6-9][0-9]{9}$')
APPROVED_EMAIL_DOMAINS = {
    'gmail.com',
    'yahoo.com',
    'yahoo.co.in',
    'outlook.com',
    'nmims.in',
}


def normalize_email(value):
    return value.strip().lower()


def validate_person_name(value):
    name = ' '.join(value.split())
    if not name:
        return False

    has_letter = False
    for character in name:
        category = unicodedata.category(character)
        if category.startswith('L'):
            has_letter = True
        elif category.startswith('M') or character in " -'’":
            continue
        else:
            return False
    return has_letter


def validate_indian_mobile(value):
    return INDIAN_MOBILE_PATTERN.fullmatch(value.strip()) is not None


def validate_email(value):
    email = normalize_email(value)
    if len(email) > 254 or not EMAIL_PATTERN.fullmatch(email):
        return False
    local_part, domain = email.rsplit('@', 1)
    return (
        len(local_part) <= 64
        and domain in APPROVED_EMAIL_DOMAINS
    )


def validate_room_number(value):
    room_number = value.strip()
    return (
        len(room_number) <= 20
        and ROOM_NUMBER_PATTERN.fullmatch(room_number) is not None
    )


def validate_password(value):
    return (
        len(value) >= 8
        and any(character.isupper() for character in value)
        and any(character.islower() for character in value)
        and any(character in '0123456789' for character in value)
        and any(not character.isalnum() and not character.isspace() for character in value)
    )
