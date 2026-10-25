
import pytest

from jarvis_core.users import GUEST, Role, UserDirectory, slugify

TOML = """
[[person]]
name = "Oliver"
role = "admin"
telegram = [111]

[[person]]
id = "mia"
name = "Mia"
role = "child"
language = "Tagalog"
telegram = [222, 333]
"""


def load(tmp_path, text, **kwargs):
    path = tmp_path / "people.toml"
    path.write_text(text, encoding="utf-8")
    return UserDirectory.from_toml(path, **kwargs)


def test_users_from_toml(tmp_path):
    users = load(tmp_path, TOML)
    assert len(users) == 2
    assert users.by_telegram(111).id == "oliver" and users.by_telegram(111).role is Role.ADMIN
    mia = users.by_telegram(333)
    assert mia.role is Role.CHILD and mia.language == "Tagalog"
    assert users.by_telegram(999) is None
    assert users.get("oliver").language == "English"


def test_v010_german_keys_and_roles_still_load(tmp_path):
    old = """
[[person]]
name = "Maria"
rolle = "erwachsen"
sprache = "Deutsch"
kalender = "maria@example.org"
morgenbrief = "aus"

[[person]]
name = "Mia"
rolle = "kind"
"""
    users = load(tmp_path, old, default_language="German")
    maria, mia = users.get("maria"), users.get("mia")
    assert maria.role is Role.ADULT and maria.language == "Deutsch"
    assert maria.calendar_id == "maria@example.org" and maria.brief_weekday is None
    assert mia.role is Role.CHILD and mia.language == "German"
    assert Role.ERWACHSEN is Role.ADULT and Role("gast") is Role.GUEST


@pytest.mark.parametrize("value", ["member", "mitglied", "adult", "erwachsen"])
def test_team_member_is_an_adult(tmp_path, value):
    users = load(tmp_path, f'[[person]]\nname = "Kim"\nrole = "{value}"\n')
    assert users.get("kim").role is Role.ADULT


def test_unknown_role_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="Unknown role for Kim"):
        load(tmp_path, '[[person]]\nname = "Kim"\nrole = "boss"\n')


def test_slack_ids(tmp_path):
    users = load(tmp_path, TOML.replace("telegram = [111]", 'telegram = [111]\nslack = ["U0OLIVER"]'))
    assert users.by_slack("U0OLIVER").id == "oliver"
    assert users.by_slack("U0NOBODY") is None


@pytest.mark.parametrize("oliver,mia,error", [
    ('["U0A"]', '["U0A"]', "assigned to two people"),
    ('["oliver"]', "[]", "Invalid Slack id"),
])
def test_duplicate_or_invalid_slack_id_is_rejected(tmp_path, oliver, mia, error):
    text = (TOML.replace("telegram = [111]", f"telegram = [111]\nslack = {oliver}")
            .replace("telegram = [222, 333]", f"telegram = [222, 333]\nslack = {mia}"))
    with pytest.raises(ValueError, match=error):
        load(tmp_path, text)


def test_duplicate_telegram_id_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        load(tmp_path, TOML.replace("[222, 333]", "[111]"))


@pytest.mark.parametrize("reserved", ["haushalt", "guest", "gast"])
def test_reserved_id_is_rejected(tmp_path, reserved):
    with pytest.raises(ValueError):
        load(tmp_path, f'[[person]]\nid = "{reserved}"\nname = "X"\n')


def test_single_user_fallback():
    users = UserDirectory.single("Jörg Müller", frozenset({5}))
    user = users.by_telegram(5)
    assert user.id == "joerg-mueller" and user.role is Role.ADMIN
    assert slugify("Ömer!") == "oemer"


def test_alexa_voices_and_admins(tmp_path):
    users = load(tmp_path, TOML.replace('telegram = [111]', 'telegram = [111]\nalexa = ["amzn1.ask.person.A"]'))
    assert users.by_alexa("amzn1.ask.person.A").id == "oliver"
    assert users.by_alexa("amzn1.ask.person.X") is None
    assert [u.id for u in users.admins()] == ["oliver"]
    assert GUEST.id == "guest" and GUEST.role is Role.GUEST
