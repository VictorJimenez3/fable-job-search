from radar.location import assess_us_location
from radar.internship import annotate as annotate_internship, gates as internship_gates
from radar.models import Job
from radar.score import gates as new_grad_gates


def test_foreign_and_remote_foreign_are_non_us():
    assert assess_us_location(["London, United Kingdom"])["status"] == "non_us"
    assert assess_us_location(["Remote, Canada"], remote=True)["status"] == "non_us"


def test_remote_alone_and_missing_locations_are_unknown():
    assert assess_us_location([], remote=True)["status"] == "unknown"
    assert assess_us_location(["Remote"], remote=True)["status"] == "unknown"
    for value in ("Remote, CA", "Remote - IN", "Remote - DE", "Remote, CO", "Remote, Georgia"):
        assert assess_us_location([value], remote=True)["status"] == "unknown", value
    for value in ("In-Office", "In-person", "Hybrid", "CA-Remote", "IN-Remote", "DE-Remote"):
        assert assess_us_location([value])["status"] == "unknown", value
    assert assess_us_location(["IA-CEDAR RAPIDS"])["status"] == "us"
    assert assess_us_location(["US-Remote"], remote=True)["status"] == "us"


def test_explicit_us_and_mixed_options_are_actionable_location_evidence():
    assert assess_us_location(["Remote, United States"], remote=True)["status"] == "us"
    mixed = assess_us_location(["London, United Kingdom", "New York, NY"])
    assert mixed["status"] == "mixed"
    assert mixed["evidence"] == ["New York, NY", "London, United Kingdom"]


def test_state_context_is_conservative_and_handles_common_cities():
    assert assess_us_location(["San Jose, CA"])["status"] == "us"
    assert assess_us_location(["Cambridge, MA"])["status"] == "us"
    assert assess_us_location(["Albuquerque, New Mexico"])["status"] == "us"
    for value in ("London, OH", "Ottawa, IL", "Rome, GA", "Toronto, KS", "Vancouver, WA",
                  "London OH", "Ottawa IL", "Toronto, Kansas", "Vancouver, Washington"):
        assert assess_us_location([value])["status"] == "us", value
    for value in ("Toronto, CA", "Bengaluru, IN", "London, ON"):
        assert assess_us_location([value])["status"] == "non_us", value
    assert assess_us_location(["CA"])["status"] == "unknown"
    assert assess_us_location(["IN"])["status"] == "unknown"
    assert assess_us_location(["CA, Canada"])["status"] == "non_us"


def test_us_aliases_regions_and_country_collisions_are_conservative():
    assert assess_us_location(["US"])["status"] == "us"
    assert assess_us_location(["Remote US"], remote=True)["status"] == "us"
    assert assess_us_location(["Remote in EMEA"], remote=True)["status"] == "non_us"
    assert assess_us_location(["Anywhere in Europe"], remote=True)["status"] == "non_us"
    assert assess_us_location(["Tbilisi, Georgia"])["status"] == "non_us"
    assert assess_us_location(["Georgia"])["status"] == "unknown"
    assert assess_us_location(["Paris, TX"])["status"] == "us"
    assert assess_us_location(["Dublin, CA"])["status"] == "us"
    assert assess_us_location(["London, UK; New York, NY"])["status"] == "mixed"
    assert assess_us_location(["United States / Canada"])["status"] == "mixed"
    assert assess_us_location(["Tbilisi, Georgia / United States"])["status"] == "mixed"
    assert assess_us_location(["Remote, excluding United States"], remote=True)["status"] == "non_us"


def test_us_schedule_or_audience_words_are_not_work_location_evidence():
    assert assess_us_location(["India (US shift)"])["status"] == "non_us"
    assert assess_us_location(["Remote - Philippines, US hours"], remote=True)["status"] == "non_us"
    assert assess_us_location(["Remote (US clients)"], remote=True)["status"] == "unknown"
    assert assess_us_location(["United States / Canada"])["status"] == "mixed"


def test_snapshot_us_aliases_and_foreign_unknowns_are_classified_conservatively():
    for value in ("NYC", "SF", "San Francisco", "New York", "New York City", "Boston",
                  "San Jose California", "IA-CEDAR RAPIDS", "Dallas (USDALFRI) Frisco AI HUB"):
        assert assess_us_location([value])["status"] == "us", value
    for value in ("Bangkok", "Kuala Lumpur", "Stevenage", "Wellington-NZL", "Rome",
                  "Seoul", "Ottawa ON", "Gratkorn"):
        assert assess_us_location([value])["status"] == "non_us", value
    assert assess_us_location(["San Jose, Costa Rica"])["status"] == "non_us"


def test_international_country_evidence_is_not_limited_to_the_original_fixture_set():
    for value in (
        "Paris, France", "Dublin, Ireland", "Lagos, Nigeria", "Seoul, South Korea",
        "Mexico City, Mexico", "New Delhi, India", "Auckland, New Zealand",
        "Santiago, Chile", "Warsaw, Poland", "Dubai, United Arab Emirates",
    ):
        assert assess_us_location([value])["status"] == "non_us", value


def test_reason_is_auditable_and_evidence_is_bounded():
    result = assess_us_location(["New York, NY", "London, United Kingdom"])
    assert result["reason"]
    assert result["evidence"] == ["New York, NY", "London, United Kingdom"]
    result = assess_us_location([f"Site {index}, New York, NY" for index in range(20)])
    assert len(result["evidence"]) <= 8


def test_cached_location_results_are_fresh_and_mutation_safe():
    first = assess_us_location(["New York, NY", "London, UK"])
    first["status"] = "poisoned"
    first["evidence"].append("poisoned")
    second = assess_us_location(["new york, ny", "london, uk"])
    assert second == {
        "status": "mixed",
        "evidence": ["new york, ny", "london, uk"],
        "reason": "US and foreign location options are both listed",
    }


def test_unknown_location_stays_visible_but_is_not_alert_eligible_in_both_lanes():
    new_grad = Job(company="Acme", title="Software Engineer, New Grad",
                   url="https://jobs.example.test/1", source="greenhouse",
                   locations=["Remote"], description="New grad early career role.")
    keep, alert, reasons = new_grad_gates(new_grad)
    assert keep and not alert
    assert new_grad.location_eligibility["status"] == "unknown"
    assert any("location unverified" in reason for reason in reasons)

    internship = Job(company="Acme", title="Software Engineering Intern",
                     url="https://jobs.example.test/2", source="greenhouse",
                     locations=[], description="Current undergraduate students may apply.")
    annotate_internship(internship)
    keep, alert, reasons = internship_gates(internship)
    assert keep and not alert
    assert internship.location_eligibility["status"] == "unknown"
    assert any("location unverified" in reason for reason in reasons)


def test_mixed_us_and_foreign_locations_keep_a_confirmed_us_option():
    posting = Job(company="Acme", title="Software Engineering Intern",
                  url="https://jobs.example.test/3", source="greenhouse",
                  locations=["London, United Kingdom", "New York, NY"],
                  description="Current undergraduate students may apply.")
    annotate_internship(posting)
    assert internship_gates(posting)[0:2] == (True, True)
