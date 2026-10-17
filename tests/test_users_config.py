
import pytest

from jarvis_core.users import Role, UserDirectory, slugify

TOML = """
[[person]]
name = "Oliver"
rolle = "admin"
telegram = [111]

[[person]]
id = "mia"
name = "Mia"
rolle = "kind"
sprache = "Tagalog"
telegram = [222, 333]
"""


def test_users_from_toml(tmp_path):
    path = tmp_path / "familie.toml"
    path.write_text(TOML, encoding="utf-8")
    users = UserDirectory.from_toml(path)
    assert len(users) == 2
    assert users.by_telegram(111).id == "oliver" and users.by_telegram(111).role is Role.ADMIN
    mia = users.by_telegram(333)
    assert mia.role is Role.KIND and mia.language == "Tagalog"
    assert users.by_telegram(999) is None


def test_duplicate_telegram_id_is_rejected(tmp_path):
    path = tmp_path / "familie.toml"
    path.write_text(TOML.replace("[222, 333]", "[111]"), encoding="utf-8")
    with pytest.raises(ValueError):
        UserDirectory.from_toml(path)


def test_reserved_id_is_rejected(tmp_path):
    path = tmp_path / "familie.toml"
    path.write_text('[[person]]\nid = "haushalt"\nname = "X"\n', encoding="utf-8")
    with pytest.raises(ValueError):
        UserDirectory.from_toml(path)


def test_single_user_fallback():
    users = UserDirectory.single("Jörg Müller", frozenset({5}))
    user = users.by_telegram(5)
    assert user.id == "joerg-mueller" and user.role is Role.ADMIN
    assert slugify("Ömer!") == "oemer"


def test_alexa_voices_and_admins(tmp_path):
    path = tmp_path / "familie.toml"
    path.write_text(TOML.replace('telegram = [111]', 'telegram = [111]\nalexa = ["amzn1.ask.person.A"]'),
                    encoding="utf-8")
    users = UserDirectory.from_toml(path)
    assert users.by_alexa("amzn1.ask.person.A").id == "oliver"
    assert users.by_alexa("amzn1.ask.person.X") is None
    assert [u.id for u in users.admins()] == ["oliver"]


def test_guest_id_is_reserved(tmp_path):
    path = tmp_path / "familie.toml"
    path.write_text('[[person]]\nid = "gast"\nname = "X"\n', encoding="utf-8")
    with pytest.raises(ValueError):
        UserDirectory.from_toml(path)
