from narration_pipeline.messages import discovery_failed_message, hold_message, summary_text
from narration_pipeline.night import ItemResult, NightReport


def row(title="Article 1", url="https://example.test/tna/a1"):
    return {"title": title, "url": url}


def result(status="published", title="T", cost=0.0, gemini=0.0, **kw):
    return ItemResult(url=f"https://example.test/{title}", title=title, status=status, outcome=None,
                      reasons=[], cost_usd=cost, gemini_usd=gemini, **kw)


def test_hold_message_names_title_and_reasons():
    title, body = hold_message(row(), ["the agent held it (03:10 garbled)", "second reason"])
    assert "Article 1" in title
    assert "the agent held it (03:10 garbled)" in body and "second reason" in body


def test_hold_message_body_has_no_query_string_or_token():
    reason = "GET https://example.test/feed?token=abc123&x=1 returned 500 and then https://a.test/p#frag"
    _, body = hold_message(row(), [reason])
    assert "?" not in body and "token=" not in body and "abc123" not in body and "#frag" not in body
    assert "https://example.test/feed returned 500" in body


def test_discovery_failed_message():
    title, body = discovery_failed_message("The New Atlantis", "no items in the feed")
    assert title == "Discovery failed: The New Atlantis" and body == "no items in the feed"


def test_summary_counts():
    report = NightReport(results=[
        result("published", "A", cost=0.20, gemini=0.06),
        result("published", "B", cost=0.10, gemini=0.03),
        result("published_with_notes", "C", cost=0.05, gemini=0.03),
        result("held", "Held One", cost=0.06, gemini=0.06),
        result("failed", "E"),
    ])
    text = summary_text(report)
    assert text.splitlines()[0] == "2 published, 1 with notes, 1 held, 1 failed"
    assert "Held One" in text
    assert "Claude $0.41, Gemini about $0.18" in text


def test_summary_omits_zero_counts():
    text = summary_text(NightReport(results=[result("published", "A", cost=0.1)]))
    assert text.splitlines()[0] == "1 published"


def test_summary_none_when_nothing_happened():
    assert summary_text(NightReport()) is None
    assert summary_text(NightReport(discovered={"tna": 0})) is None


def test_summary_lines_for_the_rest():
    report = NightReport(discovered={"a": 2, "b": 1}, discovery_errors=["feed: no items"],
                         mac_unreachable=True, stopped_at_cutoff=True)
    lines = summary_text(report).splitlines()
    assert "The Mac wasn't reachable." in lines
    assert "Stopped at the 06:00 cutoff." in lines
    assert "Discovery failed: feed: no items" in lines
    assert "3 new articles queued." in lines


def test_summary_when_only_new_articles_were_queued():
    assert summary_text(NightReport(discovered={"a": 1})) == "1 new articles queued."
