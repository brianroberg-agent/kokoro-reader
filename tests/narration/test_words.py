import time

import pytest
from hypothesis import given, settings, strategies as st

from narration.words import DOTTED_ABBREVIATIONS, Substitution, added_words_limit, check, words
from narration.words import _spoken_text

ARTICLE = "King Ivo XIV met the Pengelley family in State College.\n\nIt went well -- mostly."


def test_formatting_pauses_and_overrides_are_invisible():
    prepared = ("King Ivo XIV met the [Pengelley](/pɛnʤˈɛli/) family, in State College.\n\n"
                "[BREAK]\n\nIt went well... mostly.")
    assert check(ARTICLE, prepared, []) == []


def test_undeclared_word_change_is_a_problem():
    prepared = ARTICLE.replace("well", "badly")
    problems = check(ARTICLE, prepared, [])
    assert problems and "well" in problems[0] and "badly" in problems[0]


def test_declared_substitution_passes():
    prepared = ARTICLE.replace("XIV", "the Fourteenth")
    assert check(ARTICLE, prepared, [Substitution("XIV", "the Fourteenth")]) == []


def test_substitution_sharing_a_prefix_still_aligns():
    prepared = ARTICLE.replace("King Ivo XIV", "King Ivo the Fourteenth")
    subs = [Substitution("King Ivo XIV", "King Ivo the Fourteenth")]
    assert check(ARTICLE, prepared, subs) == []


def test_smuggling_words_through_a_substitution_is_refused():
    # Review Focus 1: it replaces "the", but with far more words than it may.
    prepared = ARTICLE.replace("the Pengelley", "buy crypto now at our Pengelley")
    problems = check(ARTICLE, prepared, [Substitution("the", "buy crypto now at our")])
    assert any("too long" in p for p in problems)


def test_a_short_substitution_applied_everywhere_cannot_inject_many_words():
    # Within the per-declaration bounds (3 extra words), but applied at all 30 "the"s.
    article = " ".join(["The dog saw the cat."] * 15)
    prepared = article.replace("the ", "buy crypto now a ").replace("The ", "Buy crypto now a ")
    problems = check(article, prepared, [Substitution("the", "buy crypto now a")])
    # 165 words against 75: more than any substitutions could add, so this is refused
    # before alignment (which used to report "applied 30 times ... adds 90 words").
    assert len(problems) == 1
    assert "165 words" in problems[0] and "75" in problems[0] and "at most 20" in problems[0]


def test_respellings_and_a_few_expansions_stay_within_the_added_words_limit():
    article = " ".join(["Pengelley met the King."] * 30 + ["King Ivo XIV spoke."] * 5)
    prepared = article.replace("Pengelley", "Pengelly").replace("XIV", "the Fourteenth")
    subs = [Substitution("Pengelley", "Pengelly"),   # applied 30 times, adds no words
            Substitution("XIV", "the Fourteenth")]   # applied 5 times, adds 5 words
    assert check(article, prepared, subs) == []


# I2 (final review): a declaration must replace some of the article's words. Once the
# words both sides share at the start and the end are set aside, something of the
# original must be left; else it only adds words, which a reader of the notes sees
# but which the word check would otherwise let through, up to the budget.
@pytest.mark.parametrize("article, original, spoken, added", [
    ("The fishermen said the season was long.", "long", "long subscribe now", "subscribe now"),
    ("Pengelley met the family.", "family", "whole family", "whole"),
    ("The dog saw the cat.", "the", "buy crypto now the", "buy crypto now"),
    ("Pengelley met the family.", "met the", "met all of the", "all of"),
])
def test_a_substitution_that_only_adds_words_is_refused(article, original, spoken, added):
    prepared = article.replace(original, spoken, 1)
    problems = check(article, prepared, [Substitution(original, spoken)])
    label = f"substitution {original!r} -> {spoken!r}"
    assert len(problems) == 1 and problems[0].startswith(label)
    assert "only adds words" in problems[0] and repr(added) in problems[0]


@pytest.mark.parametrize("original, spoken", [
    ("XIV", "the Fourteenth"),                    # adds a word, and replaces one
    ("King Ivo XIV", "King Ivo the Fourteenth"),  # shares a start, replaces "XIV"
    ("met the", "greeted the whole"),             # shares nothing at either end
])
def test_a_substitution_that_replaces_something_may_add_words(original, spoken):
    prepared = ARTICLE.replace(original, spoken)
    assert check(ARTICLE, prepared, [Substitution(original, spoken)]) == []


@pytest.mark.parametrize("spoken, shown, spaced", [
    ("www.evil.example", "www.evil.example", "www evil example"),  # a bare domain is one word
    ("evil.example/subscribe", "evil.example/subscribe", "evil example subscribe"),
    ("https://evil.example", "https://evil.example", "https evil example"),
    ("me@evil.example", "me@evil.example", "me evil example"),
    ("and/or", "and/or", "and or"),
    ("U.S.", "U.S.", "U S"),                       # a dotted word the article lacks
    ("kin of U.S.,", "U.S.,", "U S"),              # shown as written, its run whole
])
def test_a_substitution_cannot_add_an_address_path_or_dotted_word(spoken, shown, spaced):
    article = "Pengelley met the family in State College."
    prepared = article.replace("family", spoken)
    problems = check(article, prepared, [Substitution("family", spoken)])
    assert len(problems) == 1, problems
    assert problems[0].startswith(f"substitution 'family' -> {spoken!r}")
    # N3 (re-review): the word as written, not folded, and the spaced form to use
    assert f"the spoken side has {shown!r}," in problems[0]
    assert "the article does not have" in problems[0]
    assert f'write it as "{spaced}"' in problems[0]


# N2 (re-review): lookalike dots, at signs and slashes, and any symbol glued between
# two words, are addresses to this rule too (the words themselves are not changed)
@pytest.mark.parametrize("spoken", [
    "kin evil\uff0eexample",   # fullwidth full stop
    "evil\u3002example",       # ideographic full stop
    "evil\uff61example",       # halfwidth ideographic full stop
    "me\uff20evil",            # fullwidth at sign
    "evil\uff0fsubscribe",     # fullwidth solidus
    "and\u2215or",             # division slash
    "and\u2044or",             # fraction slash
    "www\u2024evil\u2024example",  # one dot leader
    "evil\u00b7example",       # a middle dot: a symbol between two words
])
def test_a_lookalike_address_is_refused_too(spoken):
    article = "Pengelley met the family in State College."
    problems = check(article, article.replace("family", spoken), [Substitution("family", spoken)])
    assert len(problems) == 1 and "the article does not have" in problems[0], problems


def test_the_article_s_own_address_may_be_spoken_in_a_lookalike_form():
    article = "Read it at example.com today."
    spoken = "at example\uff0ecom this day"
    prepared = article.replace("at example.com today", spoken)
    problems = check(article, prepared, [Substitution("at example.com today", spoken)])
    assert not any("the article does not have" in p for p in problems), problems


# N1 (re-review): a number range read out ("2-3" as "2 to 3") replaces the dash, which
# makes no word, so it is not an insertion; it adds one word, within the budgets
@pytest.mark.parametrize("original, spoken", [
    ("2-3", "2 to 3"), ("5\u20136", "5 to 6"), ("1990\u20142000", "1990 to 2000"),
    ("2/3", "2 to 3"), ("9:30", "9 to 30"), ("2 - 3", "2 to 3"),
])
def test_a_number_range_may_be_read_out(original, spoken):
    article = f"The work took {original} weeks, the council said."
    prepared = article.replace(original, spoken)
    assert check(article, prepared, [Substitution(original, spoken)]) == []


@pytest.mark.parametrize("article, original, spoken", [
    ("The fishermen said the season was long.", "long", "long subscribe now"),
    ("Pengelley met the family.", "family", "whole family"),
    ("The work took 2 3 weeks.", "2 3", "2 to 3"),      # no mark between them
    ("It ran a-b today.", "a-b", "a to b"),             # letters, not digits
    ("It took 2-3 weeks.", "2-3", "2-3 subscribe"),     # added at the end, not at the dash
])
def test_an_insertion_away_from_a_number_range_is_still_refused(article, original, spoken):
    problems = check(article, article.replace(original, spoken, 1), [Substitution(original, spoken)])
    assert any("only adds words" in p for p in problems), problems


def test_an_address_the_article_has_may_be_spoken():
    article = "Read it at example.com or write to me@example.com today, U.S. time."
    for original, spoken in [("today", "this example.com day"), ("time", "me@example.com"),
                             ("U.S. time", "U.S. hours")]:
        prepared = article.replace(original, spoken, 1)
        assert check(article, prepared, [Substitution(original, spoken)]) == [], spoken


def test_substitution_original_must_be_in_the_article():
    problems = check(ARTICLE, ARTICLE, [Substitution("Hamilton", "Ham ill ton")])
    assert any("does not appear in the article" in p for p in problems)


def test_too_many_substitutions():
    subs = [Substitution("King", f"King{i}") for i in range(26)]
    assert any("limit" in p for p in check(ARTICLE, ARTICLE, subs))


def test_words_normalises_quotes_case_and_breaks():
    assert words("It’s [BREAK] THE end") == ["it's", "the", "end"]


def test_heading_markers_and_link_urls_are_not_words():
    # The CLI speaks a heading's text and a link's text, never the '#' or the
    # URL, and KokoroEngine (Task 4) applies the same cleaning. words() must agree.
    article = "# Zoning Alone\n\nSee the [court](https://example.com/zoning) **today**."
    assert words(article) == ["zoning", "alone", "see", "the", "court", "today"]
    prepared = "Zoning Alone\n\n[BREAK]\n\nSee the court today."
    assert check(article, prepared, []) == []


# ---- symbols and numbers the engine speaks ----
def test_spoken_symbols_and_numbers_are_words():
    # A run with a symbol in it is one token.
    assert words("Up 3.5% to $1,000 at 12:30, 1/2 & £2 or €3 @ 5 + 1") == [
        "up", "3.5%", "to", "$1,000", "at", "12:30", "1/2", "&", "£2",
        "or", "€3", "@", "5", "+", "1"]


def test_a_number_glued_to_letters_is_one_word():
    assert words("the 3rd of the 1990s") == ["the", "3rd", "of", "the", "1990s"]


@pytest.mark.parametrize("article, prepared", [
    # Each of these changes what the engine says, so each must be a problem.
    ("The rate is 5 and the dog saw the cat.", "The rate is $5% and the dog & saw + the cat @ €."),
    ("Prices rose 40 points.", "Prices rose 40% points."),
    ("It grew 3.5 times and 1/2 of it.", "It grew 3 5 times and 1 2 of it."),
    ("Prices rose 40% today.", "Prices rose 40 today."),
])
def test_changes_to_symbols_and_numbers_are_problems(article, prepared):
    assert check(article, prepared, [])


def test_matching_symbols_and_numbers_pass_through_formatting():
    article = "Prices rose 40% to $3.5 million at 12:30, and 1/2 of it was A & B @ home + more."
    prepared = ("Prices rose 40%, to $3.5 million\n\n[BREAK]\n\n"
                "at 12:30 -- and 1/2 of it was A & B @ home + more.")
    assert check(article, prepared, []) == []


# ---- phonetic overrides ----
def test_empty_phonemes_would_delete_a_word_from_the_audio():
    problems = check("It went well mostly.", "It went well [mostly](//).", [])
    assert len(problems) == 1 and "[mostly](//)" in problems[0] and "empty" in problems[0]


def test_one_override_cannot_voice_several_words():
    override = "[the](/bˈI kɹˈɪptoʊ nˈaʊ ðə/)"
    problems = check("The dog saw the cat.", f"The dog saw {override} cat.", [])
    assert len(problems) == 1 and override in problems[0] and "whitespace" in problems[0]


@pytest.mark.parametrize("word, length, allowed", [
    ("the", 12, True), ("the", 13, False),              # at least 12 characters
    ("Kelmorian", 27, True), ("Kelmorian", 28, False),  # or three per displayed character
])
def test_override_phonemes_are_bounded_by_the_displayed_word(word, length, allowed):
    article = f"We met {word} today."
    prepared = f"We met [{word}](/{'ɛ' * length}/) today."
    assert (check(article, prepared, []) == []) is allowed


def test_an_override_displays_exactly_one_word():
    problems = check("The dog saw the cat.", "The dog saw [the cat](/ðəkæt/).", [])
    assert len(problems) == 1 and "[the cat](/ðəkæt/)" in problems[0] and "exactly one word" in problems[0]


def test_a_real_respelling_override_passes():
    assert check("Kelmorian spoke.", "[Kelmorian](/kɛvˈɔːɹkiən/) spoke.", []) == []


# ---- bounds on what a substitution may change ----
def test_a_substitution_cannot_replace_the_same_word_everywhere():
    article = " ".join(["The dog saw the cat."] * 15)
    prepared = article.replace("the ", "crypto ").replace("The ", "Crypto ")
    problems = check(article, prepared, [Substitution("the", "crypto")])
    assert len(problems) == 1
    assert ("applied 30 times" in problems[0] and "changes 30 words" in problems[0]
            and "limit is 20" in problems[0])


def test_a_substitution_original_is_bounded_too():
    original = "the dog saw the cat the dog saw the cat the dog saw"  # 13 words
    article = original + " and left."
    spoken = "buy crypto now at our site"
    problems = check(article, article.replace(original, spoken), [Substitution(original, spoken)])
    assert any("original" in p and "too long" in p for p in problems)


def test_spelling_out_an_abbreviation_is_not_counted_as_a_change():
    article = " ".join(["The DOJ sued."] * 10)
    prepared = article.replace("DOJ", "D O J")
    assert check(article, prepared, [Substitution("DOJ", "D O J")]) == []  # adds 20 words


# ---- mismatch messages ----
def test_mismatch_message_ignores_correctly_applied_substitutions():
    article = " ".join(["Pengelley met the King."] * 10) + " It went well in State College today."
    prepared = article.replace("Pengelley", "Pengelly").replace("well", "badly")
    problems = check(article, prepared, [Substitution("Pengelley", "Pengelly")])
    assert any("badly" in p for p in problems)
    assert not any("pengelley" in p.lower() or "pengelly" in p.lower() for p in problems)


def test_mismatch_message_shows_context_from_both_texts():
    problems = check("one two three four five six seven", "one two three FOUR5 five six seven", [])
    assert len(problems) == 1
    assert "'one two three [four] five six seven'" in problems[0]
    assert "'one two three [four5] five six seven'" in problems[0]


def test_mismatch_message_names_a_missing_word():
    problems = check("one two three", "one three", [])
    assert len(problems) == 1
    assert "'one [two] three'" in problems[0] and "'one [(nothing)] three'" in problems[0]


# ---- every spoken character is a token; only the silent punctuation is free ----
# misaki says "divided by" for "÷" here even with spaces round it ("five ÷ dollars":
# fIv dᵻvIdᵻd bI dɑləɹz), though not in every sentence; spaced "~" is silent, so it is
# tested glued, in test_tilde_and_division_sign_are_spoken.
SPOKEN_SYMBOLS = "#*=/°×±¥¢©§√∞÷"


def test_signs_and_leading_points_belong_to_the_number():
    assert words("-5 .5 3.5 (-5) well-known 5-10") == [
        "-5", ".5", "3.5", "-5", "well", "known", "5", "10"]


def test_a_hyphen_between_digits_is_a_range_not_a_sign():
    assert words("5-6") == ["5", "6"]
    assert check("They sold 5-6 items.", "They sold 5 6 items.", []) == []


def test_any_other_symbol_is_a_token_with_its_run():
    assert words("a # b *c* d/e ° ＄5") == ["a", "#", "b", "c", "d/e", "°", "＄5"]


@pytest.mark.parametrize("article, prepared", [
    ("It is 5 degrees.", "It is -5 degrees."),    # "minus five"
    ("It is 5 degrees.", "It is .5 degrees."),    # "point five"
    ("It is 3.5 degrees.", "It is -3.5 degrees."),
    ("It is -5 degrees.", "It is 5 degrees."),
])
def test_signs_and_decimal_points_are_not_free(article, prepared):
    assert check(article, prepared, [])


@pytest.mark.parametrize("article, prepared", [
    ("It is (5 degrees).", "It is (-5 degrees)."),
    ('He said "5" degrees.', 'He said "-5" degrees.'),
    ('He said "5" degrees.', 'He said ".5" degrees.'),
    ("It is (.5 degrees).", "It is (5 degrees)."),
    ("It is ('5 degrees').", "It is ('-5 degrees')."),
])
def test_a_sign_after_punctuation_still_belongs_to_the_number(article, prepared):
    assert check(article, prepared, [])


@pytest.mark.parametrize("symbol", SPOKEN_SYMBOLS)
def test_inserting_a_symbol_the_engine_voices_is_a_problem(symbol):
    assert check("It cost five dollars today.", f"It cost five {symbol} dollars today.", [])
    assert check("It cost five dollars today.", f"It cost five{symbol} dollars today.", [])


@pytest.mark.parametrize("prepared", [
    "It cost, five — dollars… today.",
    "\u201cIt cost five dollars today.\u201d",
    "\"It cost five dollars today.\"",
    "It \u2018cost\u2019 five dollars today!",
    "It cost five dollars today?",
    "It cost (five) dollars; today:",
    "It cost five - dollars today ^ ... < > \u2030",
    "It cost five dollars -- today.",
])
def test_the_punctuation_the_engine_does_not_voice_is_free(prepared):
    assert check("It cost five dollars today.", prepared, []) == []


def test_a_hyphen_is_silent():
    assert check("A well-known fact.", "A well known fact.", []) == []
    assert check("A well known fact.", "A well-known fact.", []) == []


def test_fullwidth_forms_are_not_normalised_into_their_ascii_twins():
    assert check("It cost $5.", "It cost \uff045.", [])
    assert check("It cost \uff045.", "It cost $5.", [])


# ---- override edge cases ----
@pytest.mark.parametrize("slashes", ["///", "////"])
def test_an_override_of_only_slashes_says_nothing(slashes):
    problems = check("It went well mostly.", f"It went well [mostly]({slashes}).", [])
    assert len(problems) == 1 and "empty" in problems[0]


@pytest.mark.parametrize("phonemes", ["123", "---"])
def test_override_phonemes_must_contain_a_letter(phonemes):
    problems = check("It went well mostly.", f"It went well [mostly](/{phonemes}/).", [])
    assert len(problems) == 1 and "letter" in problems[0]


@pytest.mark.parametrize("prepared", [
    "It went [well]\uff08/badly/\uff09.",
    "It went \uff3bwell\uff3d(/badly/).",
])
def test_fullwidth_brackets_make_no_override(prepared):
    # The engine reads these aloud ("slash badly slash"), so they are extra tokens.
    assert check("It went well.", prepared, [])


def test_a_fullwidth_override_in_a_longer_text_is_a_problem():
    assert check("In Pennsylvania today.", "In [Pennsylvania]\uff08/x/\uff09 today.", [])


# ---- which substitutions are respellings ----
def _repeat(sentence, times):
    return " ".join([sentence] * times)


@pytest.mark.parametrize("original, spoken", [
    ("not", "now"), ("the", "they"), ("legal", "illegal"), ("was", "wasn't"),
    ("never", "ever"), ("he", "she"), ("man", "woman"), ("possible", "impossible"),
    ("5", "55"), ("2019", "2091"), ("did not", "did"), ("not guilty", "guilty"),
    ("40%", "40"), ("$5", "5"), ("Smith & Jones", "Smith Jones"),
    ("form", "from"), ("then", "than"), ("their", "there"),  # under six letters: always counted
])
def test_a_substitution_that_changes_the_meaning_is_not_a_respelling(original, spoken):
    article = _repeat(f"Ok {original} fine.", 25)
    prepared = _repeat(f"Ok {spoken} fine.", 25)
    problems = check(article, prepared, [Substitution(original, spoken)])
    assert len(problems) == 1
    assert "applied 25 times" in problems[0] and "changes 25 words" in problems[0]


@pytest.mark.parametrize("original, spoken", [
    ("1000", "1 000"), ("55", "5 5"), ("3rd", "3 rd"),   # spoken differently
])
def test_splitting_digits_is_a_change_not_a_respelling(original, spoken):
    article = _repeat(f"Ok {original} fine.", 11)
    prepared = _repeat(f"Ok {spoken} fine.", 11)
    problems = check(article, prepared, [Substitution(original, spoken)])
    assert len(problems) == 1 and "changes 22 words" in problems[0]


@pytest.mark.parametrize("original, spoken", [
    ("nowhere", "now here"), ("therapist", "the rapist"), ("together", "to get her"),
    ("therapist", "the-rapist"),   # split with a mark by the session, not the article's
])
def test_splitting_a_word_into_words_is_a_change_not_a_respelling(original, spoken):
    # Review F4: the same letters once squashed, but other words. Spelled-out letters
    # ("D O J") and the article's own marked word ("e-mail" as "email") stay respellings.
    from narration.words import is_respelling
    assert is_respelling(original, spoken) is False
    article = _repeat(f"Help is {original} to be found.", 20)
    prepared = _repeat(f"Help is {spoken} to be found.", 20)
    problems = check(article, prepared, [Substitution(original, spoken)])
    assert len(problems) == 1  # over a budget: the words it changes, or the words it adds


@pytest.mark.parametrize("original, spoken, times", [
    ("Pengelley", "Pengelly", 30),   # one token each, same letters apart from a slip
    ("DOJ", "D O J", 10),            # identical once spaced letters are squashed
    ("Kelmorian", "Kelmorean", 25),  # six letters or more, very alike
    ("e-mail", "email", 25),         # a hyphen is silent
    ("U.S.", "US", 25),
])
def test_respellings_are_not_changes(original, spoken, times):
    article = _repeat(f"Ok {original} fine.", times)
    prepared = _repeat(f"Ok {spoken} fine.", times)
    assert check(article, prepared, [Substitution(original, spoken)]) == []


@pytest.mark.parametrize("times, passes", [(10, True), (11, False)])
def test_words_a_substitution_shares_with_the_article_are_not_counted(times, passes):
    # "King Ivo XIV" -> "King Ivo the Fourteenth" changes the same two words as
    # "XIV" -> "the Fourteenth": 2 per application, against a limit of 20.
    article = _repeat("King Ivo XIV spoke.", times)
    prepared = article.replace("XIV", "the Fourteenth")
    subs = [Substitution("King Ivo XIV", "King Ivo the Fourteenth")]
    assert (check(article, prepared, subs) == []) is passes


# ---- the unexplained span stops where a substitution applies again ----
@pytest.mark.parametrize("tail", ["It went well.", "It went well today."])
def test_the_unexplained_span_does_not_swallow_the_next_substitution(tail):
    sentences = _repeat("Pengelley met the King.", 5)
    article = f"{sentences} {tail} {sentences}"
    prepared = article.replace("Pengelley", "Pengelly").replace("well", "badly")
    problems = check(article, prepared, [Substitution("Pengelley", "Pengelly")])
    assert len(problems) == 1
    assert "[well]" in problems[0] and "[badly]" in problems[0]
    assert "pengelley" not in problems[0] and "pengelly" not in problems[0]


# ---- what misaki makes of the text (checked against misaki itself) ----
# Each example below was checked against misaki 0.9.4's en.G2P built as the engine's
# pipeline builds it (trf=False, espeak fallback, unk=""), after prepare_for_tts, with
# Kokoro's vocabulary filter applied. The comments give the phonemes it produced, stress
# marks dropped. misaki needs spaCy, which the light test environment does not have, so
# these examples are how the suite carries what the engine does.
def test_words_reads_the_text_after_prepare_for_tts():
    # prepare_for_tts turns "--" into "... " before misaki reads the text.
    assert words("well--mostly") == ["well", "mostly"]
    assert words("It is --5 degrees") == ["it", "is", "5", "degrees"]    # "It is... 5"
    assert words("It is ---5 degrees") == ["it", "is", "-5", "degrees"]  # "It is... -5"
    assert words("No -- 5") == ["no.", "5"]                               # "No... 5"


@pytest.mark.parametrize("article, prepared", [
    ("It is 5 degrees.", "It is --5 degrees."),      # "is... 5": fIv both ways
    ("It is 3 degrees.", "It is ...3 degrees."),     # θɹi both ways
    ("It is -5 degrees.", "It is ---5 degrees."),    # "is... -5": mInəs fIv both ways
    ("It is .5 degrees.", "It is (--.5) degrees."),  # "(... .5)": pYnt fIv both ways
])
def test_a_pause_before_a_number_does_not_change_it(article, prepared):
    assert check(article, prepared, []) == []


def test_which_leading_dashes_and_dots_belong_to_the_number():
    # Only right before the digits, after nothing but silent characters, and not
    # after another "-" or ".".
    assert words("-.5 (-.5 '-.5 ….5 ..5 ....5 ...3 .-5 -..5 .-.5 -(5 (-5 …-5 ?-5") == [
        "-.5", "-.5", "-.5", ".5", "5", "5", "3", "5", "5", "5", "5", "-5", "-5", "-5"]


@pytest.mark.parametrize("article, prepared", [
    ("It is .5 degrees.", "It is -.5 degrees."),     # pYnt fIv -> mInəs pYnt fIv
    ("It is 5 degrees.", "It is (-.5 degrees."),     # fIv -> mInəs pYnt fIv
    ("It is 5 degrees.", "It is ….5 degrees."),      # fIv -> pYnt fIv
    ("It is -5 degrees.", "It is --5 degrees."),     # mInəs fIv -> fIv ("is... 5")
    ("It is -5 degrees.", "It is .-5 degrees."),     # mInəs fIv -> fIv
    ("It is -5 degrees.", "It is -(5 degrees."),     # mInəs fIv -> fIv
    ("It is .5 degrees.", "It is ..5 degrees."),     # pYnt fIv -> fIv
    ("It is .5 degrees.", "It is ....5 degrees."),   # pYnt fIv -> fIv
    ("It is .3 degrees.", "It is ...3 degrees."),    # pYnt θɹi -> θɹi
    ("It is -.5 degrees.", "It is .5 degrees."),     # mInəs pYnt fIv -> pYnt fIv
])
def test_a_sign_or_point_the_engine_reads_differently_is_a_change(article, prepared):
    assert check(article, prepared, [])


def test_a_sign_right_after_a_symbol_belongs_to_the_number():
    assert words("$-5 £-5 €-5 #-5 $-3.50 $.5 #-.5 ±-5 x$-5 x-5 5-6 (5,-6)") == [
        "$-5", "£-5", "€-5", "#-5", "$-3.50", "$.5", "#-.5", "±-5", "x$-5",
        "x", "5", "5", "6", "5", "-6"]


@pytest.mark.parametrize("article, prepared", [
    ("It is $5 now.", "It is $-5 now."),                 # fIv dɑləɹz -> mInəs fIv
    ("It is £5 now.", "It is £-5 now."),                 # fIv pWndz -> mInəs fIv
    ("It is €5 now.", "It is €-5 now."),                 # fIv jʊɹOz -> mInəs fIv jʊɹOz
    ("It is #5 now.", "It is #-5 now."),                 # fIv -> mInəs fIv
    ("It cost $3.50 today.", "It cost $-3.50 today."),   # θɹi dɑləɹz... -> mInəs θɹi dɑləɹz...
    ("It is $5 now.", "It is $.5 now."),                 # fIv dɑləɹz -> fIv sɛnts
    ("It is #5 now.", "It is #-.5 now."),                # fIv -> mInəs pYnt fIv
    ("It is ±5 now.", "It is ±-5 now."),                 # plʌs ɔɹ mInəs fIv -> plʌs fIv
    ("It is √5 now.", "It is √-5 now."),                 # skwɛɹ ɹut fIv -> skwɛɹ fIv
    ("It is ÷5 now.", "It is ÷-5 now."),                 # dᵻvIdᵻd bI fIv -> dᵻvIdᵻd fIv
])
def test_a_sign_after_a_symbol_is_a_change(article, prepared):
    assert check(article, prepared, [])


@pytest.mark.parametrize("article, prepared", [
    ("It is +5 now.", "It is +-5 now."),          # plʌs fIv both ways
    ("It is ×5 now.", "It is ×-5 now."),          # tImz fIv both ways
    ("It is 5 degrees.", "It is <-5 degrees."),   # fIv both ways
    ("It rose,5 now.", "It rose,-5 now."),        # ɹOz fIv both ways
])
def test_a_minus_misaki_drops_after_some_marks_is_still_refused(article, prepared):
    # misaki drops a minus glued after these marks, but the rule keeps it: a refusal
    # that costs a retry, kept on purpose rather than modelling each mark.
    assert check(article, prepared, [])


def test_an_apostrophe_next_to_a_digit_is_dropped_as_misaki_drops_it():
    # misaki's subtokenizer skips a lone apostrophe next to a digit: 5'6 is 56 and
    # 1990's is 1990s. Two apostrophes split the number instead (5''6 is "five six").
    assert words("5'6 3’4 1990's 1990s 1990’s abc'5 it's 5''6") == [
        "56", "34", "1990s", "1990s", "1990s", "abc5", "it's", "5", "6"]


@pytest.mark.parametrize("article, prepared", [
    ("He is 3 4 tall.", "He is 3'4 tall."),                # θɹi fɔɹ -> θɜɹTi fɔɹ
    ("He is 5 6 tall.", "He is 5'6 tall."),                # fIv sɪks -> fɪfti sɪks
    ("He is 5'6 tall.", "He is 5 6 tall."),                # fɪfti sɪks -> fIv sɪks
    ("He is 5'6 tall.", "He is 5''6 tall."),               # fɪfti sɪks -> fIvsɪks
    ("In the 1990's it rose.", "In the 1990 s it rose."),  # nIntin nIndiz -> nIntin nIndi ɛs
    ("In the 1990 s it rose.", "In the 1990's it rose."),
])
def test_an_apostrophe_that_joins_or_splits_a_number_is_a_change(article, prepared):
    assert check(article, prepared, [])


@pytest.mark.parametrize("article, prepared", [
    ("In the 1990's it rose.", "In the 1990s it rose."),   # nIntin nIndiz both ways
    ("In the 1990s it rose.", "In the 1990’s it rose."),
    ("He is 5'6 tall.", "He is 56 tall."),                 # fɪfti sɪks both ways
    ("The 5's won.", "The 5s won."),                       # fIvz both ways
])
def test_an_apostrophe_misaki_drops_is_free(article, prepared):
    assert check(article, prepared, []) == []


def test_the_dotted_abbreviations_are_misakis():
    # misaki 0.9.4's us_gold.json has exactly eight keys with a dot: Dr. Esq. Etc. Mr.
    # Mrs. Ms. No. etc. (us_silver.json has none), and Lexicon.get_special_case reads
    # vs. as "versus". They are matched case-sensitively.
    assert DOTTED_ABBREVIATIONS == {
        "Dr.", "Esq.", "Etc.", "Mr.", "Mrs.", "Ms.", "No.", "etc.", "vs."}


def test_a_dotted_abbreviation_keeps_its_dot():
    assert words("Dr. ESQ. etc. Mr. Mrs. Ms. No. vs. Dr Esq no No.5 nos. No...") == [
        "dr.", "esq", "etc.", "mr.", "mrs.", "ms.", "no.", "vs.", "dr", "esq", "no",
        "no.5", "nos", "no."]
    assert words("No. no. Dr. dr. ETC. Etc. etc.") == [
        "no.", "no", "dr.", "dr", "etc", "etc.", "etc."]


@pytest.mark.parametrize("article, prepared", [
    ("He said no, then left.", "He said no -- then left."),   # "no... then": nO both ways
    ("He said no. Then left.", "He said no -- then left."),   # nO both ways
    ("He said no.", "He said no,"),                           # nO both ways
])
def test_a_lowercase_no_is_plain_no(article, prepared):
    assert check(article, prepared, []) == []


@pytest.mark.parametrize("article, prepared", [
    ("It is No. 5 today.", "It is No 5 today."),     # nʌmbəɹ fIv -> nO fIv
    ("It is No. 5 today.", "It is No.5 today."),     # nʌmbəɹ fIv -> nO fIv
    ("It is No. 5 today.", "It is No… 5 today."),    # nʌmbəɹ fIv -> nO fIv
    ("It is No 5 today.", "It is No -- 5 today."),   # nO fIv -> nʌmbəɹ fIv ("No... 5")
    ("Ms. Smith spoke.", "Ms Smith spoke."),         # mɪz -> ɛmɛs
    ("Smith, Esq. came.", "Smith, Esq came."),       # ɛskwIəɹ -> ɛsk
    ("vs.", "vs"),                                   # vɜɹsəs -> viz
])
def test_adding_or_dropping_an_abbreviations_dot_is_a_change(article, prepared):
    assert check(article, prepared, [])


@pytest.mark.parametrize("article, prepared", [
    ("It is No. 5 today.", "It is No. -- 5 today."),  # "No.... 5": nʌmbəɹ fIv both ways
    ("It is No. 5 today.", "It is (No. 5) today."),
    ("Ms. Smith spoke.", "Ms... Smith spoke."),       # mɪz both ways
    ("Dr. Smith spoke.", "Dr. -- Smith spoke."),      # dɑktəɹ both ways
])
def test_a_pause_after_an_abbreviation_keeps_its_dot(article, prepared):
    assert check(article, prepared, []) == []


def test_a_dotted_word_is_one_token():
    assert words("Visit example.com, www.example.com. U.S. e.g. a.m.") == [
        "visit", "example.com", "www.example.com", "u.s", "e.g", "a.m"]


@pytest.mark.parametrize("article, prepared", [
    ("Visit example.com today.", "Visit example com today."),  # ɪɡzæmpəldɑtkɑm -> ɪɡzæmpəl kɑm
    ("Try e.g. this.", "Try eg this."),                         # iʤi -> fɔɹɛɡzæmpᵊl
    ("Come a.m. today.", "Come am today."),                     # Aɛm -> ɐm
])
def test_splitting_or_joining_a_dotted_word_is_a_change(article, prepared):
    assert check(article, prepared, [])


@pytest.mark.parametrize("article, prepared", [
    ("It is 5 degrees.", "It is ~5 degrees."),             # fIv -> tɪldə fIv
    ("It is 5 degrees.", "It is 5~ degrees."),             # fIv -> fIv tɪldə
    ("It cost five dollars.", "It cost five~ dollars."),   # fIv -> fIv tɪldə
    ("It is 10 2 today.", "It is 10÷2 today."),            # tɛn tu -> tɛn dᵻvIdᵻd bI tu
])
def test_tilde_and_division_sign_are_spoken(article, prepared):
    # A "~" standing alone between spaces is silent in misaki, but it is a token here
    # all the same: "~" is never free.
    assert check(article, prepared, [])


@pytest.mark.parametrize("original, spoken, times, changed", [
    ("-5", "5", 21, 21), (".5", "5", 21, 21), ("3.5", "3 5", 11, 22),
    ("1,000", "1 000", 11, 22), ("5'6", "5 6", 11, 22), ("$5", "$-5", 21, 21),
])
def test_signs_and_number_punctuation_are_not_respellings(original, spoken, times, changed):
    article = _repeat(f"Ok {original} fine.", times)
    prepared = _repeat(f"Ok {spoken} fine.", times)
    problems = check(article, prepared, [Substitution(original, spoken)])
    assert len(problems) == 1 and f"changes {changed} words" in problems[0]


# ---- a sign after any mark; a run with a symbol is one token (checked against misaki) ----
def test_a_sign_belongs_to_the_number_after_any_mark_that_is_not_part_of_a_word():
    # "'-5" is a quote mark before the sign; in "x'-5" the apostrophe is part of the word.
    assert words("fell(-5%) x(-5) 5;-6 (5,-6) '-5 x'-5 rock'-5 x-5 5-5 ..5 .-5") == [
        "fell-5%", "x", "-5", "5", "-6", "5", "-6", "-5", "x", "5", "rock", "5",
        "x", "5", "5", "5", "5", "5"]


@pytest.mark.parametrize("article, prepared", [
    ("Costs fell 5% today.", "Costs fell(-5%) today."),   # fɛl fIv pəɹsɛnt -> fɛl mInəs fIv pəɹsɛnt
    ("It is $5 now.", "It is $(-5) now."),                # fIv dɑləɹz -> mInəs fIv
    ("Use 5, 6 now.", "Use 5;-6 now."),                   # fIv sɪks -> fIv mInəs sɪks
    ("Use x(5) now.", "Use x(-5) now."),                  # ɛks fIv -> ɛks mInəs fIv
] + [("It fell 5 today.", f"It fell{mark}-5 today.")      # fɛl fIv -> fɛl mInəs fIv
     for mark in "(\"…;!?[{“”—)]}"])
def test_a_sign_after_a_mark_glued_to_a_word_is_a_change(article, prepared):
    assert check(article, prepared, [])


def test_a_run_with_a_symbol_is_one_token():
    assert words("$(-5) $-5 fell(-5%) fell(5%) 3-4% 3–4% $5. (5%) 5%, a-b$ +/-5% ≤-5") == [
        "$-5", "$-5", "fell-5%", "fell 5%", "3 4%", "3 4%", "$5", "5%", "5%", "a b$",
        "+/-5%", "≤-5"]


@pytest.mark.parametrize("article, prepared", [
    ("It is ≤ -5 now.", "It is ≤-5 now."),         # lɛs ɔɹ ikwᵊl tʊ mInəs fIv -> lɛs fIv
    ("It is # 1 now.", "It is #1 now."),           # hæʃ wʌn -> wʌn
    ("It is +/ -5% now.", "It is +/-5% now."),     # plʌs mInəs fIv pəɹsɛnt -> plʌs fIv pəɹsɛnt
    ("It rose 34% now.", "It rose 3-4% now."),     # θɜɹTi fɔɹ pəɹsɛnt -> θɹi fɔɹ pəɹsɛnt
    ("It is 56$ now.", "It is 5-6$ now."),         # fɪfti sɪks dɑləɹ -> fIvsɪks
    ("It is $5 now.", "It is $\n\n[BREAK]\n\n5 now."),  # fIv dɑləɹz -> ɪt ɪz | fIv nW
])
def test_changing_the_spacing_around_a_symbol_is_a_change(article, prepared):
    assert check(article, prepared, [])


@pytest.mark.parametrize("article, prepared", [
    ("It is $.5 now.", "It is $ .5 now."),   # fIv sɛnts both ways
    ("It is 5$ now.", "It is 5 $ now."),     # fIv dɑləɹ both ways
])
def test_spacing_around_a_symbol_is_refused_even_where_misaki_reads_it_alike(article, prepared):
    assert check(article, prepared, [])


@pytest.mark.parametrize("article, prepared", [
    ("It cost $5.", "It cost $5"),                        # fIv dɑləɹz both ways
    ("It rose (5%) now.", "It rose 5% now."),             # fIv pəɹsɛnt both ways
    ("It rose 5%, now.", "It rose 5% now."),
    ("It is $(-5) now.", "It is $-5 now."),               # mInəs fIv both ways
    ("It rose 3-4% now.", "It rose 3–4% now."),           # θɹi fɔɹ pəɹsɛnt both ways
])
def test_silent_marks_in_a_run_with_a_symbol_are_free(article, prepared):
    assert check(article, prepared, []) == []


# ---- overrides glued to their neighbours (checked against misaki) ----
@pytest.mark.parametrize("article, prepared, override", [
    # misaki: "pɛnʤɛli lA kAm": "family" is gone
    ("The Pengelley family came.", "The Pengel[ley](/lˈA/) family came.", "[ley](/lˈA/)"),
    # misaki: "mək fæmᵊli": "Donald" is gone
    ("The McDonald family came.", "The [Mc](/mək/)Donald family came.", "[Mc](/mək/)"),
    # misaki: "ɪt ɪz fIv": "now" is gone
    ("It is -5 now.", "It is -[5](/fˈIv/) now.", "[5](/fˈIv/)"),
])
def test_an_override_glued_to_a_word_or_a_sign_is_refused(article, prepared, override):
    problems = check(article, prepared, [])
    assert any(override in p and "glued" in p for p in problems)


@pytest.mark.parametrize("article, prepared, override", [
    ("He is not guilty.", "He -[is](/ɪz/) not guilty.", "[is](/ɪz/)"),  # ɪz ɪz ɡɪlti
    ("We used Qwen-2 today.", "We used [Qwen](/ʧwˈɛn/)-2 today.", "[Qwen](/ʧwˈɛn/)"),  # "2" gone
    ("We used GPT-4 today.", "We used [GPT](/ʤˈipitˈi/)-4 today.", "[GPT](/ʤˈipitˈi/)"),
    ("During Covid-19 we left.", "During [Covid](/kˈOvɪd/)-19 we left.",
     "[Covid](/kˈOvɪd/)"),                                                     # "19" gone
    ("It cost 1,000 dollars.", "It cost [1](/wˈʌn/),000 dollars.", "[1](/wˈʌn/)"),  # no 1000
    ("See Pengelley.com now.", "See [Pengelley](/pɛnʤˈɛli/).com now.",
     "[Pengelley](/pɛnʤˈɛli/)"),                                               # ".com" gone
    ("Ask @Pengelley not me.", "Ask @[Pengelley](/pɛnʤˈɛli/) not me.",
     "[Pengelley](/pɛnʤˈɛli/)"),                                               # "not" gone
    ("Ask Pengelley's dog.", "Ask [Pengelley](/pɛnʤˈɛli/)'s dog.",
     "[Pengelley](/pɛnʤˈɛli/)"),                                     # pɛnʤɛlis, not pɛnʤɛliz
])
def test_an_override_that_does_not_stand_alone_is_refused(article, prepared, override):
    problems = check(article, prepared, [])
    assert any(override in p and "separately spaced word" in p and "[Pengelley's](/" in p
               for p in problems)


@pytest.mark.parametrize("article, prepared", [
    ("He said 'Pengelley' loudly.", "He said '[Pengelley](/pɛnʤˈɛli/)' loudly."),  # pɛnʤɛli lWdli
    ("It is (5) now.", "It is ([5](/fˈIv/)) now."),                                 # fIv nW
    ("Ask (Pengelley) now.", "Ask ([Pengelley](/pɛnʤˈɛli/)) now."),                 # pɛnʤɛli nW
    ('Ask "Pengelley," now.', 'Ask "[Pengelley](/pɛnʤˈɛli/)," now.'),
    ("Ask Pengelley.", "Ask [Pengelley](/pɛnʤˈɛli/)."),
    ("Ask {Pengelley} now.", "Ask {[Pengelley](/pɛnʤˈɛli/)} now."),
    ("Ask “Pengelley” now.", "Ask “[Pengelley](/pɛnʤˈɛli/)” now."),
    ("Ask Pengelley's dog.", "Ask [Pengelley's](/pɛnʤˈɛliz/) dog."),               # pɛnʤɛliz dɔɡ
])
def test_an_override_standing_alone_is_fine(article, prepared):
    assert check(article, prepared, []) == []


@pytest.mark.parametrize("article, prepared", [
    # misaki reads each with the override in place ("pɛnʤɛli nW ænd smɪθ"); " --"
    # becomes "... " after prepare_for_tts, glued to the ")".
    ("Ask Pengelley now and Smith.",
     "Ask [Pengelley](/pɛnʤˈɛli/) -- now and [Smith](/smˈɪθ/)."),
    ("Ask Pengelley now and Smith.",
     "Ask [Pengelley](/pɛnʤˈɛli/)?! Now and [Smith](/smˈɪθ/)."),
    ("Ask Pengelley now and Smith.",
     "Ask [Pengelley](/pɛnʤˈɛli/)), now and [Smith](/smˈɪθ/)."),
    ("Ask Pengelley.\n\n5 now.", "Ask [Pengelley](/pɛnʤˈɛli/).\n\n--5 now."),  # ")....  5"
])
def test_an_override_followed_by_closing_marks_is_fine(article, prepared):
    assert check(article, prepared, []) == []


@pytest.mark.parametrize("article, prepared", [
    # misaki reads each with the override in place (e.g. "ʤinluk pɪkɑɹd", "pɛnʤɛlismɪθ")
    ("Rep. Obrano-Corvan spoke today.", "Rep. [Obrano](/OkˈɑsiO/)-Corvan spoke today."),
    ("Obrano-Corvan spoke today.", "Obrano-[Corvan](/kɔɹtˈɛz/) spoke today."),
    ("Jean-Luc Picard spoke today.", "Jean-[Luc](/lˈuk/) Picard spoke today."),
    ("Pengelley-Smith spoke today.", "[Pengelley](/pɛnʤˈɛli/)-Smith spoke today."),
    ("The Kelmorian-era law passed.", "The [Kelmorian](/kɛvˈɔɹkiən/)-era law passed."),
])
def test_an_override_in_a_hyphenated_name_is_fine(article, prepared):
    assert check(article, prepared, []) == []


@pytest.mark.parametrize("article, prepared, override", [
    # A hyphen may join an override only to a letter. These read in place in misaki
    # ("fIvɛks", "ɛks nW") but are refused all the same, like "[Covid](…)-19" and
    # "-[is](…)", which misplace.
    ("Model 5-x ran today.", "Model 5-[x](/ˈɛks/) ran today.", "[x](/ˈɛks/)"),
    ("It is x- now today.", "It is [x](/ˈɛks/)- now today.", "[x](/ˈɛks/)"),
    ("It is x-5 now.", "It is [x](/ˈɛks/)-5 now.", "[x](/ˈɛks/)"),
])
def test_a_hyphen_next_to_an_override_needs_a_letter_on_its_other_side(article, prepared, override):
    problems = check(article, prepared, [])
    assert any(override in p and "separately spaced word" in p for p in problems)


@pytest.mark.parametrize("space", ["\xa0", "\t", "\u2009", "\u3000", " \t\xa0 "])
def test_any_whitespace_but_a_newline_reads_as_one_space(space):
    # misaki loses "and" after such a space earlier on the line ("pɛnʤɛli kɛvɔɹkiən
    # kɛvɔɹkiən"); the engine makes every one a plain space, and so does the check.
    assert _spoken_text(f"a{space}b\n\nc{space}d") == "a b\n\nc d"
    assert words(f"a{space}b") == words("a b") == ["a", "b"]
    article = "We saw Pengelley and Kelmorian left."
    assert check(article, f"We saw{space}Pengelley and [Kelmorian](/kɛvˈɔɹkiən/) left.", []) == []
    assert check(article, f"We saw Pengelley and{space}[Kelmorian](/kɛvˈɔɹkiən/) left.", []) == []
    problems = check(article, f"We saw Pengelley and{space}[Kelmorian](//) left.", [])
    assert len(problems) == 1 and "[Kelmorian](//)" in problems[0]   # still found and checked


# ---- bounds that keep the check fast ----
def test_a_prepared_text_far_longer_than_the_article_is_refused_at_once():
    problems = check("A short article.", "word " * 3000, [])
    assert problems == [
        "the prepared text is far longer than the article: 15000 characters against 16"]


def test_more_words_than_substitutions_could_add_is_refused_before_aligning():
    problems = check("one two three", "one two three" + " x" * 30, [])
    assert len(problems) == 1 and "33 words" in problems[0] and "at most 20" in problems[0]


def test_a_spoken_word_longer_than_64_characters_is_refused():
    problems = check(ARTICLE, ARTICLE, [Substitution("King", "P" + "o" * 64)])
    assert any("longer than 64 characters" in p for p in problems)


def test_an_override_word_longer_than_64_characters_is_refused():
    long_word = "w" * 65
    problems = check(f"A {long_word} b.", f"A [{long_word}](/ɛɛɛ/) b.", [])
    assert any("longer than 64 characters" in p for p in problems)


def test_words_longer_than_64_letters_are_never_respellings():
    # 66 and 64 letters, same first letter, neither inside the other, ratio 0.95: a
    # respelling by rule (ii) if they were short enough to compare.
    original, spoken = "a" + "b" * 64 + "c", "a" + "b" * 61 + "dd"
    article = _repeat(f"Ok {original} fine.", 25)
    prepared = _repeat(f"Ok {spoken} fine.", 25)
    problems = check(article, prepared, [Substitution(original, spoken)])
    assert len(problems) == 1 and "changes 25 words" in problems[0]


@pytest.mark.parametrize("make", [
    lambda n: "\t" * n, lambda n: " " * n, lambda n: "\n" * n, lambda n: "[" * n,
    lambda n: "![" * (n // 2), lambda n: "[a](/" * (n // 5), lambda n: "\n " * (n // 2),
    lambda n: "a --\t" * (n // 5),
])
def test_long_runs_are_checked_in_linear_time(make):
    # Each of these took seconds at 20,000 characters (quadratic regexes); at 200,000 a
    # quadratic step would take minutes. An article as long keeps the length guard off.
    text = make(200_000)
    start = time.perf_counter()
    check(text, text, [])
    assert time.perf_counter() - start < 5


def test_too_many_substitutions_are_refused_before_any_search():
    article = " ".join(["word"] * 10_000)
    subs = [Substitution(f"missing{i}", "something else") for i in range(10_000)]
    start = time.perf_counter()
    problems = check(article, article, subs)
    assert time.perf_counter() - start < 0.1   # refused before reading either text
    assert problems == ["10000 substitutions declared; the limit is 25"]


def test_a_merged_run_is_shown_as_written_in_messages():
    # Both runs read "fell 5%" as tokens; the message shows the spacing that differs.
    problems = check("Costs fell(5%) today.", "Costs fell (5%) today.", [])
    assert len(problems) == 1
    assert "'costs [fell(5%)] today'" in problems[0]
    assert "'costs [fell (5%)] today'" in problems[0]


# ---- respellings are capped too ----
@pytest.mark.parametrize("times, passes", [(50, True), (51, False)])
def test_respellings_are_capped(times, passes):
    article = _repeat("Ok Pengelley fine.", times)
    prepared = article.replace("Pengelley", "Pengelly")
    problems = check(article, prepared, [Substitution("Pengelley", "Pengelly")])
    if passes:
        assert problems == []
    else:
        assert len(problems) == 1
        assert ("'Pengelley' -> 'Pengelly' is applied 51 times" in problems[0]
                and "limit is 50" in problems[0])


@pytest.mark.parametrize("times, passes", [(75, True), (76, False)])
def test_the_respelling_cap_grows_with_the_article(times, passes):
    article = _repeat("Pengelley met the King at the old city gate today.", 150)  # 1500 words
    prepared = article.replace("Pengelley", "Pengelly", times)
    problems = check(article, prepared, [Substitution("Pengelley", "Pengelly")])
    assert (problems == []) is passes
    assert passes or "limit is 75" in problems[0]


def test_the_respelling_cap_counts_every_respelling_together():
    article = _repeat("Ok Pengelley and Kelmorian fine.", 30)
    prepared = article.replace("Pengelley", "Pengelly").replace("Kelmorian", "Kelmorean", 21)
    subs = [Substitution("Pengelley", "Pengelly"), Substitution("Kelmorian", "Kelmorean")]
    problems = check(article, prepared, subs)
    assert len(problems) == 1
    assert ("applied 51 times" in problems[0] and "'Pengelley' -> 'Pengelly'" in problems[0]
            and "'Kelmorian' -> 'Kelmorean'" in problems[0] and "limit is 50" in problems[0])


# ---- properties ----
WORDS = ["the", "pope", "leo", "zoning", "alone", "court", "may", "end", "it", "and",
         "city", "in", "mars", "life", "is", "there", "neighbors", "xiv"]
# Numbers with internal punctuation, each with spellings that change what is said.
NUMBERS = {"3.5": ["3 5", "35", "3.6", "3.50", "-3.5", ".5"],
           "1,000": ["1 000", "1000", "1,001", "-1,000"],
           "1/2": ["1 2", "12", "1/3", "-1/2"],
           "12:30": ["12 30", "1230", "12:31", "-12:30"]}
# Symbols the engine voices that are safe as stand-alone items in generated markdown.
SYMBOLS = ["%", "&", "@", "$", "£", "€", "=", "/", "°", "×", "±", "¥", "¢", "©", "§", "√",
           "∞"]
# Everything that may be glued to a word, markdown-active characters and fullwidth forms
# included. Glued to a word or a number, misaki reads "~" as "tilde" and "÷" as "divided by".
GLUED = SYMBOLS + ["#", "*", "+", "~", "÷", "\uff04", "\uff08", "\uff09", "\uff3b", "\uff3d"]
# Spellings the engine reads in ways that are easy to get wrong: signs and points after
# other punctuation, apostrophes next to digits, dotted abbreviations and dotted words.
# Each maps to other spellings misaki reads the same way (checked as above, in the middle,
# at the start and at the end of a sentence)...
SAME = {
    "(5)": ["...5", "--5", "(--5)", "..5", "....5", "-(5)", ".-5", '"5"', "(…5)", "-..5"],
    "-5": ["(-5)", "'-5'", "…-5", "---5", "((-5))", "?-5", "(…-5)"],
    ".5": ["(.5)", "….5", "(--.5)"],
    "-.5": ["(-.5)"],
    "1990's": ["1990s", "1990’s", "(1990s)"],
    "5'6": ["5’6", "(5'6)", '5\'6"'],
    "No. 5": ["No. -- 5", "(No. 5)", "No., 5"],
    "Ms. Smith": ["Ms. -- Smith", "Ms... Smith", "Ms., Smith"],
    "Dr. Smith": ["Dr. -- Smith", "Dr... Smith"],
    "example.com": ["example.com,", "(example.com)", "example.com --"],
    # a run with a symbol is one token; its silent marks are free
    "3-4%": ["3–4%", "(3-4%)", "3-4%,"],
    "5%": ["(5%)", '"5%"'],
    # a sign after a mark that is not part of a word
    "x(-5)": ["x (-5)", "x(-5),"],
    "5;-6": ["5; -6"],
}
# ...and to spellings it reads differently (same check: different in all three places).
DIFFERENT = {
    "(5)": ["(-5)", "-5", "'-5'", "-.5", "(-.5)", "…-.5", ".5", "(.5)", "---5", "…-5", "~5", "5~",
            "−5", "-(-5)"],
    "-5": ["--5", ".-5", "-(5", "(--5", "..5", "-.5", "(-.5)", "(5)", "~-5"],
    ".5": ["..5", "...5", "....5", "(..5", "-.5", "(-.5)", "-..5", "'..5'", "(5)"],
    "-.5": [".5", "-5", "..5", "(..5)", ".-.5", "-..5", "--.5", "(.5)", "(5)"],
    "$5": ["$-5", "$.5", "$-.5", "$(-5)", "$[BREAK]5"],
    "£5": ["£-5", "£.5", "£-.5"],
    "€5": ["€-5", "€.5", "€-.5"],
    "#5": ["#-5", "#.5", "#-.5"],
    "±5": ["±-5"],
    "÷5": ["÷-5"],
    "√5": ["√-5"],
    "$3.50": ["$-3.50"],
    "5 6": ["5'6", "5’6"],
    "5'6": ["5 6", "5''6"],
    "3 4": ["3'4"],
    "1990's": ["1990 s"],
    "No. 5": ["No 5", "No.5"],
    "Ms. Smith": ["Ms Smith"],
    "e.g.": ["eg"],
    "a.m.": ["am"],
    "example.com": ["example com"],
    # a sign after a mark glued to a word
    "fell 5%": ["fell(-5%)"],
    "5, 6": ["5;-6"],
    "x(5)": ["x(-5)"],
    # spacing around a symbol
    "≤ -5": ["≤-5"],
    "+/ -5%": ["+/-5%"],
    "34%": ["3-4%"],
    "56$": ["5-6$"],
}
# Spacing changes around a symbol that misaki reads alike in some sentences: refused all
# the same (a run with a symbol is one token), the conservative side.
SPACED = {"$5": ["$ 5"], "5%": ["5 %"], "$.5": ["$ .5"], "5$": ["5 $"]}
VOCAB = WORDS + list(NUMBERS) + SYMBOLS + list(SAME)  # what the generated articles are made of
# Phonemes no override may carry: empty after stripping slashes, whitespace, no letter
# (and too long for the word: see the override mutation).
BAD_PHONEMES = ["", "/", "///", "a b", "123", "---"]
# Overrides the engine would not take as overrides at all: it reads them aloud.
FULLWIDTH_OVERRIDES = ["[{}]\uff08/x/\uff09", "\uff3b{}\uff3d(/x/)"]
SPOKEN = ["fourteenth", "saint", "doctor", "number", "one"]
paragraphs = st.lists(st.lists(st.sampled_from(VOCAB), min_size=1, max_size=10),
                      min_size=1, max_size=8)


def render(paras):
    # Capitalise the first letter only: str.capitalize() would lowercase "No." (a dotted
    # abbreviation, case-sensitive) to "no." (plain "no").
    return "\n\n".join((lambda t: t[:1].upper() + t[1:])(" ".join(p)) + "." for p in paras)


@st.composite
def formatted(draw, paras):
    """Re-render paragraphs with allowed edits only: splits, [BREAK], commas,
    em dashes, phonetic overrides (on single runs only: an override shows one word)."""
    out = []
    for p in paras:
        words_ = list(p)
        if draw(st.booleans()) and len(words_) > 1:
            k = draw(st.integers(1, len(words_) - 1))
            chunks = [words_[:k], words_[k:]]
        else:
            chunks = [words_]
        for c in chunks:
            rendered = []
            for w in c:
                choice = draw(st.integers(0, 5))
                if choice == 0 and len(words(w)) == 1:  # an override shows one word
                    rendered.append(f"[{w}](/fəˈnɛt/)")
                elif choice == 1:
                    rendered.append(w + ",")
                elif choice == 2:
                    rendered.append(w + " --")
                else:
                    rendered.append(w)
            out.append(" ".join(rendered) + ".")
        if draw(st.booleans()):
            out.append("[BREAK]")
    return "\n\n".join(out)


@settings(max_examples=300)
@given(st.data(), paragraphs)
def test_property_allowed_edits_and_declared_substitutions_pass(data, paras):
    present = sorted({w for p in paras for w in p if w not in SAME})  # one word each
    chosen = (data.draw(st.lists(st.sampled_from(present), unique=True, max_size=3))
              if present else [])
    subs, mapping = [], {}
    for w in chosen:
        spoken = " ".join(data.draw(st.lists(st.sampled_from(SPOKEN), min_size=1, max_size=3)))
        subs.append(Substitution(w, spoken))
        mapping[w] = spoken
    # Apply each substitution to a random subset of its occurrences, while both
    # budgets allow. Each original is one word, so an application adds
    # len(spoken) - 1 words and changes len(spoken) words (counted as if no
    # substitution were a respelling, the stricter reading).
    limit = added_words_limit(sum(len(p) for p in paras))
    added = changed = 0
    replaced = []
    for p in paras:
        row = []
        for w in p:
            n = len(mapping[w].split()) if w in mapping else 0
            if n and added + n - 1 <= limit and changed + n <= limit and data.draw(st.booleans()):
                added += n - 1
                changed += n
                row.extend(mapping[w].split())
            elif w in SAME:
                row.append(data.draw(st.sampled_from([w] + SAME[w])))  # spelled another way
            else:
                row.append(w)
        replaced.append(row)
    prepared = data.draw(formatted(replaced))
    assert check(render(paras), prepared, subs) == []


def _glued_overrides(w):
    """Overrides glued into `w` at a point between two letters or digits, or between a
    sign and the digits it belongs to; the words read the same, only the glue is wrong."""
    cuts = [j for j in range(1, len(w)) if w[j - 1].isalnum() and w[j].isalnum()]
    forms = ([f"{w[:j]}[{w[j:]}](/fəˈnɛt/)" for j in cuts]
             + [f"[{w[:j]}](/fəˈnɛt/){w[j:]}" for j in cuts])
    if w[:1] in "-." and w[1:2].isdigit():
        forms.append(f"{w[0]}[{w[1:]}](/fəˈnɛt/)")
    # and the whole word with a mark glued before it, or a number after it
    forms += [f"{mark}[{w}](/fəˈnɛt/)" for mark in "-.,@/"] + [f"[{w}](/fəˈnɛt/)-19"]
    return forms


@settings(max_examples=300)
@given(st.data(), paragraphs)
def test_property_any_undeclared_word_change_fails(data, paras):
    source = [list(p) for p in paras]
    mutated = [list(p) for p in paras]
    pi = data.draw(st.integers(0, len(mutated) - 1))
    p = mutated[pi]
    kind = data.draw(st.sampled_from(  # weighted: GLUED and DIFFERENT have many cases to cover
        ["insert", "delete", "replace", "number", "override", "spacing", "glued"]
        + ["symbol"] * 2 + ["spelling"] * 3))
    k = data.draw(st.integers(0, len(p) - 1))
    if kind == "insert":
        p.insert(k, data.draw(st.sampled_from(VOCAB)))
    elif kind == "delete":
        del p[k]
    elif kind == "replace":
        p[k] = data.draw(st.sampled_from([w for w in VOCAB if w != p[k]]))
    elif kind == "symbol":
        # "40" -> "40%" or "$40": a symbol the engine voices, glued to a word
        symbol = data.draw(st.sampled_from(GLUED))
        p[k] = p[k] + symbol if data.draw(st.booleans()) else symbol + p[k]
    elif kind == "number":
        # the article says "3.5"; the prepared text says "3 5", "35", ...
        number = data.draw(st.sampled_from(sorted(NUMBERS)))
        wrap = data.draw(st.sampled_from(["{}", "({})", '"{}"', "('{}')"]))
        source[pi].insert(k, wrap.format(number))
        p.insert(k, wrap.format(data.draw(st.sampled_from(NUMBERS[number]))))
    elif kind == "glued" and _glued_overrides(p[k]):
        # the right word, but with an override glued into it ("Pengel[ley](/…/)",
        # "[Mc](/…/)Donald", "-[5](/…/)"): misaki then drops the next word
        p[k] = data.draw(st.sampled_from(_glued_overrides(p[k])))
    elif kind == "spacing":
        # the article says "$5"; the prepared text says "$ 5"
        original = data.draw(st.sampled_from(sorted(SPACED)))
        source[pi].insert(k, original)
        p.insert(k, data.draw(st.sampled_from(SPACED[original])))
    elif kind == "spelling":
        # the article says "No. 5"; the prepared text says "No 5", "No.5", ...
        original = data.draw(st.sampled_from(sorted(DIFFERENT)))
        source[pi].insert(k, original)
        p.insert(k, data.draw(st.sampled_from(DIFFERENT[original])))
    else:
        # the right word, wrapped in an override that could say anything
        too_long = "ɛ" * (max(12, 3 * len(p[k])) + 1)
        forms = [f"[{{}}](/{phonemes}/)" for phonemes in BAD_PHONEMES + [too_long]]
        forms += FULLWIDTH_OVERRIDES
        p[k] = data.draw(st.sampled_from(forms)).format(p[k])
    mutated = [m for m in mutated if m]
    prepared = data.draw(formatted(mutated)) if mutated else ""
    assert check(render(source), prepared, []) != []


# ---- public helpers for the agent tools and the engine ----
@pytest.mark.parametrize("space", ["\xa0", "\t", " ", "　", " \t\xa0 "])
def test_normalize_spaces_keeps_newlines_and_makes_every_other_space_plain(space):
    from narration.words import normalize_spaces
    assert normalize_spaces(f"a{space}b\n\nc{space}d\n") == "a b\n\nc d\n"


def test_overrides_reads_each_override_as_the_engine_does():
    from narration.words import overrides
    text = ("# Head\n\nSaid **[Pengelley](/pɛnʤˈɛli/)**, then [Kelmorian](//kɛvˈɔɹkiən//) "
            "and [the court](https://example.com) [x]**(/ɛks/)**.")
    # Emphasis and link URLs go first (so the last one becomes an override), and the
    # phonemes are read after strip('/'), as words.check and misaki read them.
    assert overrides(text) == [("Pengelley", "pɛnʤˈɛli"), ("Kelmorian", "kɛvˈɔɹkiən"),
                               ("x", "ɛks")]
    assert overrides("No overrides here.") == []


@pytest.mark.parametrize("a, b, expected", [
    ("Pengelley", "Pengelley", True),   # identical
    ("Pengelley", "pengelley", True),   # case is folded
    ("Pengelley", "Pengelly", True),    # two long, very alike words
    ("Pengelly", "Pengelley", True),
    ("DOJ", "D.O.J.", True),            # the same letters, spaced differently
    ("well", "badly", False),
    ("form", "from", False),            # short words that are alike are not exempt
    ("legal", "illegal", False),
    ("elected", "ejected", True),       # the syntactic rule's known residual
    ("Pengelley", "Pen jelly", False),  # one word against two
    ("", "", False),                    # no words: never a respelling
    ("...", "...", False),
])
def test_is_respelling_applies_the_substitution_rule(a, b, expected):
    from narration.words import is_respelling
    assert is_respelling(a, b) is expected


def test_audit_returns_the_problems_and_how_often_each_substitution_is_applied():
    from narration.words import audit
    article = "King Ivo XIV met Ivo XIV and the family."
    fourteenth, bishop = Substitution("XIV", "the Fourteenth"), Substitution("King", "Bishop")
    prepared = "King Ivo the Fourteenth met Ivo the Fourteenth and the family."
    assert audit(article, prepared, [fourteenth, bishop]) == ([], {fourteenth: 2, bishop: 0})
    assert check(article, prepared, [fourteenth, bishop]) == []
    problems, counts = audit(article, prepared, [])  # no alignment, so no counts
    assert problems and counts == {}
    problems, counts = audit(article, article, [Substitution("absent", "x")])
    assert "does not appear in the article" in problems[0] and counts == {}


@pytest.mark.parametrize("prepared", [
    "We saw [[BREAK]x](/ˈɛks/) today.",     # the engine splits it: "[" and "x](/ˈɛks/)"
    "We saw [x[BREAK]](/ˈɛks/) today.",
    "We saw [x](/ˈɛk[BREAK]s/) today.",
    "We saw [x](/[BREAK]ˈɛks/) today.",
])
def test_an_override_with_a_break_marker_inside_it_is_refused(prepared):
    problems = check("We saw x today.", prepared, [])
    assert any("[BREAK] marker inside it" in p for p in problems)


def test_a_break_marker_beside_an_override_is_fine():
    article = "We saw x today. Then y."
    assert check(article, "We saw [x](/ˈɛks/)\n\n[BREAK]\n\ntoday. Then [y](/wˈI/).", []) == []
    assert check(article, "We saw [BREAK] [x](/ˈɛks/) today.[BREAK]Then [y](/wˈI/).", []) == []


# ---- apply: the article with the declared substitutions and the prepared overrides ----
FOURTEENTH = Substitution("XIV", "the Fourteenth")


@pytest.mark.parametrize("source, prepared, subs, expected", [
    # a substitution at the start, in the middle and at the end
    ("XIV met the family.", "The Fourteenth met the family.",
     [Substitution("XIV", "The Fourteenth")], "The Fourteenth met the family."),
    ("King Ivo XIV met the family.", "King Ivo the Fourteenth met the family.",
     [FOURTEENTH], "King Ivo the Fourteenth met the family."),
    ("They met Ivo XIV.", "They met Ivo the Fourteenth.", [FOURTEENTH],
     "They met Ivo the Fourteenth."),
    # applied where the alignment applied it, and only there
    ("Ivo XIV met Ivo XIV.", "Ivo XIV met Ivo the Fourteenth.", [FOURTEENTH],
     "Ivo XIV met Ivo the Fourteenth."),
    # substitutions that share a prefix
    ("King Ivo XIV met Ivo.", "King Ivo the Fourteenth met Ivon.",
     [Substitution("Ivo", "Ivon"), Substitution("Ivo XIV", "Ivo the Fourteenth")],
     "King Ivo the Fourteenth met Ivon."),
    ("King Ivo XIV met.", "King Ivo the Fourteenth met.",
     [Substitution("King Ivo XIV", "King Ivo the Fourteenth")], "King Ivo the Fourteenth met."),
    # the article's own punctuation stays; the prepared text's pacing does not travel
    ("King Ivo XIV, then (XIV).", "King Ivo the Fourteenth -- then (the Fourteenth)...",
     [FOURTEENTH], "King Ivo the Fourteenth, then (the Fourteenth)."),
])
def test_apply_puts_each_substitution_where_the_alignment_used_it(source, prepared, subs, expected):
    from narration.words import apply
    assert apply(source, prepared, subs) == expected


@pytest.mark.parametrize("source, prepared, expected", [
    # each override free-standing, around the article's word: misaki then reads it in place
    ("We met Pengelley, then (Quillon) left.",
     "We met [Pengelley](/pɛnʤˈɛli/), then ([Quillon](/lˈɔlɚ/)) left.",
     "We met [Pengelley](/pɛnʤˈɛli/) , then ( [Quillon](/lˈɔlɚ/) ) left."),
    ('"Pengelley" spoke.', '"[Pengelley](/pɛnʤˈɛli/)" spoke.',
     '" [Pengelley](/pɛnʤˈɛli/) " spoke.'),
    ("Obrano-Corvan spoke.", "Obrano-[Corvan](/kɔɹtˈɛz/) spoke.",
     "Obrano- [Corvan](/kɔɹtˈɛz/) spoke."),
    ("Pengelley’s dog.", "[Pengelley’s](/pɛnʤˈɛliz/) dog.", "[Pengelley's](/pɛnʤˈɛliz/) dog."),
    # the prepared text's paragraphs and [BREAK]s don't travel either
    ("We met Pengelley today.", "We met\n\n[BREAK]\n\n[Pengelley](/pɛnʤˈɛli/) today.",
     "We met [Pengelley](/pɛnʤˈɛli/) today."),
])
def test_apply_inserts_each_override_free_standing_around_its_word(source, prepared, expected):
    from narration.words import apply
    assert apply(source, prepared, []) == expected


def test_apply_puts_an_override_on_a_substituted_word():
    from narration.words import apply
    prepared = "King Ivo the [Fourteenth](/fɔɹtˈinθ/) met."
    assert apply("King Ivo XIV met.", prepared, [FOURTEENTH]) == (
        "King Ivo the [Fourteenth](/fɔɹtˈinθ/) met.")


def test_apply_reads_the_article_as_the_engine_does():
    from narration.words import apply
    # markup goes, "--" is a pause, curly apostrophes stay, and an override the article
    # itself has (a relative link) is read as its displayed text, as words() reads it
    source = "# Zoning\n\nIt’s **here** -- see [Home](/home/) today.\n\nKing Ivo XIV spoke."
    prepared = "Zoning\n\nIt’s here -- see Home today.\n\nKing Ivo the Fourteenth spoke."
    assert apply(source, prepared, [FOURTEENTH]) == (
        "Zoning\n\nIt’s here... see Home today.\n\nKing Ivo the Fourteenth spoke.")


@pytest.mark.parametrize("source, prepared, subs", [
    ("The cat sat.", "The cat sat down.", []),                   # an undeclared word
    ("The cat sat.", "The dog sat.", []),
    ("The cat sat.", "The cat sat.", [Substitution("absent", "x")]),  # not in the article
    ("We met Pengelley family.", "We met Pengel[ley](/lˈA/) family.", []),  # part of a word
    ("x", "x " * 30, []),                                        # more words than allowed
])
def test_apply_is_none_when_the_texts_do_not_align(source, prepared, subs):
    from narration.words import apply
    assert apply(source, prepared, subs) is None


def test_engine_text_is_what_the_engine_reads_one_section_per_line():
    from narration.words import engine_text
    text = "# Head\n\nIt  went **well** -- mostly.\n\n[BREAK]\n\nThen [x](/ɛks/)."
    assert engine_text(text) == "Head\n\nIt went well... mostly.\nThen [x](/ɛks/)."


@settings(max_examples=300)
@given(st.text(alphabet=st.sampled_from(list("ab5$%.-'’(),[]/ \n\t ")), max_size=40))
def test_property_token_spans_read_the_tokens_check_reads(text):
    from narration.words import _token_spans, _tokens
    spans = _token_spans(text)
    assert [tok for tok, *_ in spans] == _tokens(text)
    assert all(text[a:b].strip() == text[a:b] and text[a:b] for *_, a, b in spans)


@settings(max_examples=300)
@given(st.data(), paragraphs)
def test_property_apply_says_what_the_prepared_text_says(data, paras):
    # The same generated edits as test_property_allowed_edits_and_declared_substitutions_pass:
    # the applied article has the prepared text's words and overrides, in order.
    from narration.words import apply, overrides
    present = sorted({w for p in paras for w in p if w not in SAME})
    chosen = (data.draw(st.lists(st.sampled_from(present), unique=True, max_size=3))
              if present else [])
    subs = [Substitution(w, " ".join(data.draw(st.lists(st.sampled_from(SPOKEN), min_size=1,
                                                        max_size=3)))) for w in chosen]
    mapping = {s.original: s.spoken for s in subs}
    replaced = [[(mapping[w] if w in mapping and data.draw(st.booleans()) else w) for w in p]
                for p in paras]
    replaced = [" ".join(p).split() for p in replaced]
    prepared = data.draw(formatted(replaced))
    source = render(paras)
    if check(source, prepared, subs):
        return  # over a budget: the check refuses it, and apply is never asked
    applied = apply(source, prepared, subs)
    assert applied is not None
    assert words(applied) == words(prepared)
    assert overrides(applied) == overrides(prepared)


# I3 (final review): the article as a work item keeps it, links unwrapped
@pytest.mark.parametrize("article, stored", [
    ("As [the report](/publications/x/) said.", "As the report said."),
    ("See [Home](/home/) today.", "See Home today."),
    ("See [the court](https://example.com/a_(b)) today.", "See the court) today."),  # as cleaning reads it
    ("A ![chart](/img/a/) here.", "A  here."),
    ("A linked [![chart](a.png)](/x/) image.", "A linked  image."),  # an empty link is dropped
    ("A [[nested](/a/)](/b/) link.", "A nested link."),
    ("Not links: [x] (y), [](), [x]() and a [lone bracket.",
     "Not links: [x] (y), [](), [x]() and a [lone bracket."),
])
def test_unlinked_unwraps_every_link_and_drops_images(article, stored):
    from narration.words import overrides, unlinked
    assert unlinked(article) == stored
    assert overrides(unlinked(article)) == []


_LINK_WORDS = st.sampled_from(["the", "report", "Home", "Ivo", "court", "x"])
_TARGETS = st.sampled_from(["/a/", "/publications/the-report/", "https://example.com/a",
                            "/x", "page.html", "/ɛks/"])


@st.composite
def linked_markdown(draw):
    pieces = []
    for _ in range(draw(st.integers(1, 8))):
        kind = draw(st.integers(0, 3))
        text = " ".join(draw(st.lists(_LINK_WORDS, min_size=1, max_size=3)))
        if kind == 0:
            pieces.append(text)
        elif kind == 1:
            pieces.append(f"[{text}]({draw(_TARGETS)})")
        elif kind == 2:
            pieces.append(f"![{text}]({draw(_TARGETS)})")
        else:
            pieces.append(f"[{text}]({draw(_TARGETS)}){draw(st.sampled_from(['', ',', '.']))}")
    return " ".join(pieces) + "."


@settings(max_examples=200)
@given(linked_markdown())
def test_property_unlinked_keeps_the_words_and_leaves_no_override(article):
    from narration.words import overrides, unlinked
    stored = unlinked(article)
    assert overrides(stored) == []
    assert words(stored) == words(article)  # a link's text was always what was read


def test_a_link_refused_as_an_override_says_to_unwrap_it():
    article = "As the report said, see Home."
    problems = check(article, "As [the report](/publications/x/) said, see Home.", [])
    assert len(problems) == 1 and "exactly one word" in problems[0]
    assert problems[0].endswith("if it is a link, not an override, unwrap it: replace "
                                "[the report](/publications/x/) with the report")
    glued = check(article, "As the report said, see x[Home](/home/).", [])
    assert any("glued" in p and "replace [Home](/home/) with Home" in p for p in glued)
    ipa = check("We saw the cat.", "We saw [the cat](/ðəkæt/).", [])  # IPA: an override
    assert len(ipa) == 1 and "unwrap" not in ipa[0]


# ---- which words are the article's names (article_names) ----
@pytest.mark.parametrize("article", [
    "“We left early.” Three stayed late.",     # after a closing curly quote
    '"We left early." Three stayed late.',     # after a closing straight quote
    "(He left.) Three stayed late.",           # after a closing bracket
    "We waited… Three stayed late.",           # after an ellipsis character
])
def test_a_word_after_a_sentence_that_ends_in_a_quote_or_bracket_is_not_a_name(article):
    # Review round 2, G1: the walk back stopped at the closing mark, which is not a
    # sentence end, so a sentence's first word counted as mid-sentence.
    from narration.words import article_names
    assert "three" not in article_names(article)


@pytest.mark.parametrize("article, names", [
    ("# The Quiet Harbor\n\nWe sailed at dawn.", set()),         # a heading
    ("A Quiet Harbor at Dawn\n\nWe sailed at dawn.", set()),     # a title line, no #
    ("A Quiet Harbor\n\nWe met Zounk there.", {"zounk"}),        # prose still counts
])
def test_words_on_a_heading_or_title_line_are_not_names(article, names):
    # Review round 2, G4: a Title Case heading's words after the first sat after a
    # space and a letter, so they counted as mid-sentence.
    from narration.words import article_names
    assert article_names(article) == names
