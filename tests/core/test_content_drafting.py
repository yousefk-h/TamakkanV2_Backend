"""Base-prompt fixes:

- card #16: the drafting system prompt must never assume a video exists,
  and must give distinct guidance per the existing Level system rather
  than one generic line for every level.
- card #18: the prompt must frame the model as a prompt designer (not
  Mariam herself), explicitly require every lesson-specific detail to come
  from the given source material, and explicitly forbid the exact generic
  failure-mode topics a real production prompt drifted into (sports,
  animals, fashion, cars) as a class, not just as individual banned
  phrases.
- card #19: the grounded-but-compressed-paragraph output was still too thin
  to reliably steer a live avatar. Dropped the earlier rule that
  `avatar_prompt` be "woven into natural prose rather than a labeled list" -
  the system prompt itself is allowed to be structured (only Mariam's
  spoken replies must stay plain) - and required the full
  role/lesson-info/objectives/rules/dynamic-stage-flow/interaction-behavior/
  adaptive-difficulty/speaking-style/content-reference/success-criteria/
  final-instruction structure, a 5,000-9,000 character depth target, and
  explicit cross-field consistency (opening_line matches Stage 1;
  warm_up_questions and vocabulary come from the same lesson).
"""

from app.core.content_drafting import SYSTEM_PROMPT


def test_system_prompt_never_mentions_watching_or_reading_the_source():
    lowered = SYSTEM_PROMPT.lower()
    for banned in ("did you watch", "what did you see", "the video you watched", "did you read"):
        assert banned not in lowered


def test_system_prompt_does_not_hardcode_video_as_the_source():
    lowered = SYSTEM_PROMPT.lower()
    assert "from a lesson video" not in lowered
    assert "what this video is about" not in lowered
    assert "source material" in lowered


def test_system_prompt_covers_every_existing_level_distinctly():
    lowered = SYSTEM_PROMPT.lower()
    for level in ("beginner", "intermediate", "advanced"):
        assert level in lowered


def test_system_prompt_frames_the_model_as_a_prompt_designer_not_mariam():
    lowered = SYSTEM_PROMPT.lower()
    assert "instructional designer" in lowered
    assert "not a conversation partner" in lowered


def test_system_prompt_forbids_the_known_generic_failure_topics_as_a_class():
    """These exact words (sports/animals/fashion/cars) are only acceptable
    here as part of the *prohibition* - never as suggested/encouraged
    content. If a lesson genuinely is about one of them, that word would
    come from the teacher's own source material at generation time, not
    from this static prompt.
    """
    lowered = SYSTEM_PROMPT.lower()
    for banned_example in ("sports", "animals", "fashion", "cars"):
        assert banned_example in lowered  # named explicitly as a forbidden default
    # And named as a class of anti-pattern, not just scattered individually.
    assert "generic icebreaker" in lowered or "generic filler" in lowered or "ideas about language learning" in lowered


def test_system_prompt_requires_every_structural_section():
    """card #19: the full hand-written-quality-reference structure, not a
    compressed paragraph - every section must be named as its own heading.
    """
    for heading in (
        "# ROLE & PERSONALITY",
        "# LESSON INFORMATION",
        "# LEARNING OBJECTIVES",
        "# CRITICAL CONVERSATION RULES",
        "# CONVERSATION FLOW",
        "# INTELLIGENT INTERACTION BEHAVIOR",
        "# ADAPTIVE DIFFICULTY",
        "# SPEAKING STYLE",
        "# LESSON CONTENT REFERENCE",
        "# SUCCESS CRITERIA",
        "# FINAL OVERRIDING INSTRUCTION",
    ):
        assert heading in SYSTEM_PROMPT


def test_system_prompt_explicitly_allows_structure_in_the_system_prompt_itself():
    """card #19's central change: structure in `avatar_prompt` is now
    required, not merely tolerated - and the old "woven into natural prose"
    rule must be gone.
    """
    lowered = SYSTEM_PROMPT.lower()
    assert "allowed, and expected, to be structured" in lowered
    assert "woven into natural prose" not in lowered


def test_system_prompt_requires_dynamically_generated_stages_not_a_fixed_set():
    lowered = SYSTEM_PROMPT.lower()
    assert "never reuse a fixed set of stage names across lessons" in lowered
    assert "usually 4 to 8" in lowered


def test_system_prompt_requires_the_interaction_behavior_cases():
    lowered = SYSTEM_PROMPT.lower()
    for case in (
        "one-word answer",
        "\"i don't know\"",
        "goes silent",
        "makes a mistake",
        "long, rich answer",
        "asks mariam a question",
        "goes off topic",
        "is doing well",
    ):
        assert case in lowered


def test_system_prompt_requires_cross_field_consistency():
    lowered = SYSTEM_PROMPT.lower()
    assert "must match how stage 1" in lowered
    assert "no contradictions" in lowered


def test_system_prompt_gives_a_character_length_target():
    """The target must stay comfortably under `MAX_PROMPT_LENGTH`
    (app/schema/limits.py, 10,000) even allowing for a real model's typical
    overshoot past an instructed length - a draft that can never be
    re-saved through `PATCH /topics/{id}` (which does enforce that schema
    cap) is a regression, not a depth improvement.
    """
    from app.schema.limits import MAX_PROMPT_LENGTH

    assert "4,000 to 7,000 characters" in SYSTEM_PROMPT
    assert "8,000 characters" in SYSTEM_PROMPT
    assert MAX_PROMPT_LENGTH == 10_000  # the hard constraint this guidance must stay under


def test_system_prompt_requires_the_title_to_be_used():
    assert "title" in SYSTEM_PROMPT.lower()


def test_system_prompt_requests_only_the_final_output():
    lowered = SYSTEM_PROMPT.lower()
    assert "no preamble" in lowered
