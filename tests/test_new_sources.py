"""Parser tests for v2 adapters, using handcrafted fixtures shaped like each
API's documented/observed responses. Live behavior is verified in CI runs."""
from unittest.mock import patch

from radar.discovery import extract
from radar.sources import ats, bigco


def test_greenhouse_records_the_official_board_source():
    payload = {"jobs": [{"title": "AI Engineer", "absolute_url": "https://example.test/job/1",
                           "location": {"name": "New York, NY"}, "content": ""}]}
    entry = {"name": "Fanatics", "ats": "greenhouse", "token": "fanaticsinc"}
    with patch.object(ats, "get_json", return_value=payload):
        job = ats.fetch_greenhouse(entry)[0]
    assert job.source_url == "https://job-boards.greenhouse.io/fanaticsinc"


def test_eightfold_parses_positions():
    payload = {"positions": [
        {"id": 1, "name": "Software Engineer, New Grad", "location": "Los Gatos, CA",
         "locations": ["Los Gatos, CA", "Remote, US"], "t_create": 1783300000,
         "canonicalPositionUrl": "https://explore.jobs.netflix.net/careers/job/1"},
    ], "totalJobs": 1}
    entry = {"name": "Netflix", "ats": "eightfold", "token": "netflix",
             "extra": {"host": "https://explore.jobs.netflix.net", "domain": "netflix.com"}}
    with patch.object(ats, "get_json", return_value=payload):
        jobs = ats.fetch_eightfold(entry)
    assert len(jobs) == 1
    assert jobs[0].company == "Netflix" and jobs[0].posted_at == 1783300000
    assert "Remote, US" in jobs[0].locations


def test_icims_parses_jobs():
    payload = {"jobs": [{"jobTitle": "Entry Level Software Engineer", "jobId": "73366",
                         "jobLocation": "Manassas, VA"}]}
    entry = {"name": "GD Mission Systems", "ats": "icims", "token": "careers-gdms", "extra": {}}
    with patch.object(ats, "get_json", return_value=payload):
        jobs = ats.fetch_icims(entry)
    assert jobs[0].url == "https://careers-gdms.icims.com/jobs/73366/job"


def test_oracle_orc_parses_requisitions():
    payload = {"items": [{"requisitionList": [
        {"Id": "210767161", "Title": "Data Scientist Associate",
         "PrimaryLocation": "New York, NY", "PostedDate": "2026-07-08"}]}]}
    entry = {"name": "JPMorgan Chase", "ats": "oracle_orc", "token": "jpmc",
             "extra": {"host": "jpmc.fa.oraclecloud.com", "site": "CX_1001"}}
    with patch.object(ats, "get_json", return_value=payload):
        jobs = ats.fetch_oracle_orc(entry)
    assert jobs[0].title == "Data Scientist Associate"
    assert "CX_1001" in jobs[0].url and jobs[0].posted_at is not None


def test_phenom_parses_widgets_response():
    payload = {"refineSearch": {"data": {"jobs": [
        {"jobSeqNo": "JNJUS123", "title": "Data Science New Grad",
         "cityStateCountry": "New Brunswick, NJ, US",
         "applyUrl": "https://www.careers.jnj.com/job/JNJUS123",
         "postedDate": "2026-07-09T00:00:00Z"}]}}}
    entry = {"name": "Johnson & Johnson", "ats": "phenom", "token": "jnj",
             "extra": {"host": "https://www.careers.jnj.com", "refnum": "jnjjnj"}}
    with patch.object(ats, "post_json", return_value=payload):
        jobs = ats.fetch_phenom(entry, queries=["new grad"])
    assert jobs[0].company == "Johnson & Johnson"
    assert jobs[0].posted_at is not None


def test_tesla_filters_departments():
    payload = {"listings": [
        {"id": 1, "t": "Software Engineer, Autopilot", "dp": "3", "l": "7"},
        {"id": 2, "t": "Service Advisor", "dp": "9", "l": "7"},
    ], "lookup": {"departments": {"3": "Autopilot & Robotics Engineering",
                                  "9": "Sales & Customer Support"},
                  "locations": {"7": "Palo Alto, California"}}}
    with patch.object(bigco, "get_json", return_value=payload):
        jobs = bigco.fetch_tesla({})
    assert len(jobs) == 1
    assert jobs[0].title == "Software Engineer, Autopilot"


def test_amazon_dedupes_across_queries():
    payload = {"jobs": [{"id_icims": "A1", "title": "SDE I", "job_path": "/en/jobs/A1",
                         "normalized_location": "Seattle, WA", "posted_date": "July 9, 2026"}]}
    with patch.object(bigco, "get_json", return_value=payload):
        jobs = bigco.fetch_amazon({})
    assert len(jobs) == 1  # same job returned for all 3 queries → one Job
    assert jobs[0].posted_at is not None


def test_microsoft_parses_nested_result():
    payload = {"operationResult": {"result": {"jobs": [
        {"jobId": "17123", "title": "Software Engineer", "postingDate": "2026-07-09T00:00:00Z",
         "properties": {"locations": ["Redmond, Washington, United States"]}}]}}}
    with patch.object(bigco, "get_json", return_value=payload):
        jobs = bigco.fetch_microsoft({})
    assert len(jobs) == 1
    assert jobs[0].url.endswith("/job/17123")


def test_apple_parses_search_results():
    payload = {"searchResults": [
        {"positionId": "200591234", "postingTitle": "Software Engineer - Early Career",
         "transformedPostingTitle": "software-engineer", "postingDate": "2026-07-08",
         "locations": [{"name": "Cupertino"}], "homeOffice": False}]}
    with patch.object(bigco, "post_json", return_value=payload), \
         patch.object(bigco, "_apple_csrf", return_value=dict(bigco.BROWSER_HEADERS)):
        jobs = bigco.fetch_apple({})
    assert len(jobs) == 1
    assert "200591234" in jobs[0].url


def test_extract_new_ats_patterns():
    assert extract("https://careers-gdms.icims.com/jobs/73366/job?mobile=true") == \
        ("icims", "careers-gdms", {})
    got = extract("https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/210767161")
    assert got == ("oracle_orc", "jpmc", {"host": "jpmc.fa.oraclecloud.com", "site": "CX_1001"})


def test_all_new_fetchers_registered():
    for name in ("eightfold", "icims", "oracle_orc", "phenom",
                 "tesla", "amazon", "microsoft", "apple", "google"):
        assert name in ats.FETCHERS
