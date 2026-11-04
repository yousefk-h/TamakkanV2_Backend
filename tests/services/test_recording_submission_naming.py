"""`_slugify`/`_build_public_id` - the Cloudinary public_id naming scheme,
tested directly since the interesting behavior (transliteration, the
"untitled" fallback) is easy to get subtly wrong and doesn't need a real DB.
"""

from app.models.topic_content import Level, TopicContent
from app.models.user import Role, User
from app.services.recording_submission_service import _build_public_id, _slugify


def test_slugify_lowercases_and_hyphenates():
    assert _slugify("Talking about Music") == "talking-about-music"


def test_slugify_strips_punctuation():
    assert _slugify("Sarah O'Connor") == "sarah-o-connor"
    assert _slugify("Écoute! Français #1") == "ecoute-francais-1"


def test_slugify_falls_back_to_untitled_when_nothing_survives():
    assert _slugify("   ") == "untitled"
    assert _slugify("!!!") == "untitled"
    # Non-Latin scripts have no ASCII transliteration - a known, accepted
    # limitation (localization is deferred platform-wide), not a bug to fix
    # in a folder-naming helper.
    assert _slugify("مرحبا") == "untitled"


async def test_build_public_id_is_in_the_tamakkan_folder_under_the_students_name():
    student = User(email="s@example.com", password_hash="h", full_name="Sarah O'Connor", role=Role.STUDENT)
    topic = TopicContent(title="Talking about Music!", level=Level.INTERMEDIATE)

    public_id = _build_public_id(student, topic)

    assert public_id.startswith(f"Tamakkan/sarah-o-connor-{student.id[:8]}/talking-about-music-")


async def test_build_public_id_folders_differ_for_students_with_the_same_name():
    """`_slugify` is many-to-one, so the name alone is not an identity. Two
    students called "John Doe" - or any two students with non-Latin names, which
    both slugify to "untitled" - must not share a folder. `full_name` is also
    Student-writable via `PATCH /me`, so a name-only folder would let a student
    aim their recordings at another student's folder.
    """
    topic = TopicContent(title="Talking about Music", level=Level.INTERMEDIATE)
    first = User(email="a@example.com", password_hash="h", full_name="John Doe", role=Role.STUDENT)
    second = User(email="b@example.com", password_hash="h", full_name="john  doe!", role=Role.STUDENT)

    first_folder = _build_public_id(first, topic).rsplit("/", 1)[0]
    second_folder = _build_public_id(second, topic).rsplit("/", 1)[0]

    assert first_folder != second_folder
    # ...but the readable part is still there, which is the whole point of
    # using the name at all rather than a bare id.
    assert first_folder.startswith("Tamakkan/john-doe-")
    assert second_folder.startswith("Tamakkan/john-doe-")


async def test_build_public_id_is_unique_across_calls():
    """Same student, same topic, called twice - must not collide, since
    Cloudinary silently overwrites on a duplicate public_id.
    """
    student = User(email="s@example.com", password_hash="h", full_name="Sarah Connor", role=Role.STUDENT)
    topic = TopicContent(title="Talking about Music", level=Level.INTERMEDIATE)

    first = _build_public_id(student, topic)
    second = _build_public_id(student, topic)

    assert first != second
