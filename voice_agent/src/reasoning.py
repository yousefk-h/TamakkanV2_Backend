"""Keeps a model's internal reasoning out of what Mariam says, shows and records.

Mariam's reply must be only the final learner-facing answer. Some models narrate their own planning in the reply text
("(Wait... the student said 'Okay,' ...)"). This filter sits on the LLM's text stream, BEFORE text-to-speech, the live
caption and the trusted transcript all see it, so a discarded passage is never spoken, shown or scored.

What is removed - only what is clearly hidden planning, never ordinary punctuation:

1. Explicit reasoning blocks the provider or model marks with tags: `<think>…</think>`, `<thinking>`, `<thought>`,
   `<reasoning>`, `<scratchpad>` and Gemma's `<|channel>thought … <channel|>`.
2. A parenthetical that is clearly self-talk about the learner or the plan: it refers to "the student/learner/user" in the
   third person, or opens with a planning cue ("Wait…", "Hmm…", "Let me think…", "I should…", "I will give a hint…").
   Parentheses that are normal teaching speech - "(Smiling)", "(for example: …)", "(British spelling)" - are left alone.
3. The narration sandwiched between such parentheticals ("(Wait…) I'm still waiting for X. (Since the student… ) <answer>"):
   after a hidden parenthetical, the text that follows is held; if another hidden parenthetical follows it, the held
   narration was part of the same monologue and is dropped too. Otherwise it is released unchanged. A paragraph break or
   a long run ends the hold, so ordinary replies are never swallowed.

Nothing is held for ordinary replies: text streams straight through except while a "(" is still open (a few tokens) and
after a hidden parenthetical was found (the rest of that short reply, which is then released at the end). Plain Python, no
LiveKit imports, so it is unit-testable.
"""

import re

# (start, end) markers, matched case-insensitively.
TAG_PAIRS: tuple[tuple[str, str], ...] = (
    ("<think>", "</think>"),
    ("<thinking>", "</thinking>"),
    ("<thought>", "</thought>"),
    ("<reasoning>", "</reasoning>"),
    ("<scratchpad>", "</scratchpad>"),
    ("<|channel>thought", "<channel|>"),
)
_TAG_STARTS = tuple(start for start, _ in TAG_PAIRS)

# A parenthetical longer than this is not stage direction or an aside: stop waiting for its ")" and treat "(" as plain text.
MAX_PAREN_CHARS = 400
# After a hidden parenthetical, the narration that follows is held at most this long before it is released as ordinary text.
MAX_HOLD_CHARS = 300

_LEARNER_THIRD_PERSON = re.compile(r"\b(?:the|this|that) (?:student|learner|user)\b|\bthe (?:student|learner|user)'s\b", re.I)
_PLANNING_CUE = re.compile(
    r"^(?:wait\b|hmm+\b|let me (?:think|see|check|consider)\b|let's (?:think|see)\b|i should\b|i need to\b|i must\b"
    r"|i(?:'ll| will) (?:give|ask|wait|try|offer)\b|since (?:they|he|she)\b|they (?:haven't|have not|didn't|did not|said|asked|seem)\b"
    r"|note to self\b|internal\b|thinking\b)",
    re.I,
)


def is_hidden_reasoning(inner: str) -> bool:
    """Whether the text inside a parenthesis is the model talking to itself rather than to the learner."""
    text = " ".join(inner.split())
    if len(text) < 12:
        return False
    return bool(_LEARNER_THIRD_PERSON.search(text) or _PLANNING_CUE.match(text))


def _partial_marker_length(text: str, markers: tuple[str, ...]) -> int:
    lowered = text.lower()
    return max(
        (n for marker in markers for n in range(1, min(len(text), len(marker) - 1) + 1) if lowered.endswith(marker[:n])),
        default=0,
    )


class ReasoningFilter:
    """Streaming filter: `feed()` each text piece, `flush()` at the end of the reply. One instance per reply."""

    def __init__(self) -> None:
        self._buffer = ""
        self._end_tag: str | None = None  # inside a tagged reasoning block until this marker
        self._holding = False  # a hidden parenthetical was just dropped: hold the narration that follows
        self._held = ""
        self._started = False
        self.dropped_chars = 0

    # -- public ----------------------------------------------------------------------------------------------------------

    def feed(self, text: str) -> str:
        self._buffer += text
        return self._lead(self._drain(final=False))

    def flush(self) -> str:
        out = self._drain(final=True)
        if self._held:
            out += self._held
            self._held = ""
        self._holding = False
        return self._lead(out)

    # -- internals -------------------------------------------------------------------------------------------------------

    def _lead(self, out: str) -> str:
        """Drops the whitespace a removed passage leaves at the very start of the reply."""
        if not self._started:
            out = out.lstrip()
            if out:
                self._started = True
        return out

    def _plain(self, text: str, out: list[str]) -> None:
        if not text:
            return
        if not self._holding:
            out.append(text)
            return
        self._held += text
        if "\n\n" in self._held or len(self._held) > MAX_HOLD_CHARS:
            out.append(self._held)
            self._held = ""
            self._holding = False

    def _drain(self, *, final: bool) -> str:
        out: list[str] = []
        while self._buffer:
            if self._end_tag is not None:
                idx = self._buffer.lower().find(self._end_tag)
                if idx < 0:
                    keep = _partial_marker_length(self._buffer, (self._end_tag,))
                    self.dropped_chars += len(self._buffer) - keep
                    self._buffer = self._buffer[len(self._buffer) - keep :] if keep else ""
                    break
                self.dropped_chars += idx + len(self._end_tag)
                self._buffer = self._buffer[idx + len(self._end_tag) :]
                self._end_tag = None
                continue

            lowered = self._buffer.lower()
            tag_at = min((i for i in (lowered.find(s) for s in _TAG_STARTS) if i >= 0), default=-1)
            paren_at = self._buffer.find("(")
            candidates = [i for i in (tag_at, paren_at) if i >= 0]
            if not candidates:
                keep = 0 if final else _partial_marker_length(self._buffer, _TAG_STARTS)
                cut = len(self._buffer) - keep
                self._plain(self._buffer[:cut], out)
                self._buffer = self._buffer[cut:]
                break

            nxt = min(candidates)
            self._plain(self._buffer[:nxt], out)
            self._buffer = self._buffer[nxt:]

            if tag_at == nxt:
                lowered = self._buffer.lower()
                start = next(s for s in _TAG_STARTS if lowered.startswith(s))
                self._end_tag = dict(TAG_PAIRS)[start]
                self.dropped_chars += len(start)
                self._buffer = self._buffer[len(start) :]
                continue

            close = self._matching_paren(self._buffer)
            if close < 0:
                if final or len(self._buffer) > MAX_PAREN_CHARS:
                    self._plain("(", out)  # not an aside: ordinary text that happens to contain "("
                    self._buffer = self._buffer[1:]
                    continue
                break  # wait for the rest of the parenthetical
            block, self._buffer = self._buffer[: close + 1], self._buffer[close + 1 :]
            if is_hidden_reasoning(block[1:-1]):
                self.dropped_chars += len(block) + len(self._held)
                self._held = ""  # the narration between two hidden parentheticals was part of the same monologue
                self._holding = True
            else:
                if self._holding:
                    out.append(self._held)
                    self._held = ""
                    self._holding = False
                out.append(block)
        return "".join(out)

    @staticmethod
    def _matching_paren(text: str) -> int:
        depth = 0
        for i, ch in enumerate(text):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return i
        return -1
