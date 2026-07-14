"""Tests for response-rate analytics in strategist memo."""
import time

from radar import strategist


def test_response_rates_empty_inputs():
    """With no applied jobs, return empty stats."""
    result = strategist.response_rates([], {})
    assert result == {"by_sector": {}, "by_source": {}}


def test_response_rates_single_applied_no_yet_responded():
    """An 'applied' job with no response shows in count but not responded."""
    now = int(time.time())
    jobs = {
        "j1": {"id": "j1", "company": "Tempus", "sector": "healthtech",
               "source": "greenhouse"}
    }
    applied = [
        {"id": "j1", "applied_at": now - 3 * 86400, "stage": "applied"}
    ]
    result = strategist.response_rates(applied, jobs)
    assert result["by_sector"]["healthtech"] == {"applied": 1, "responded": 0}
    assert result["by_source"]["greenhouse"] == {"applied": 1, "responded": 0}


def test_response_rates_oa_counts_as_responded():
    """OA stage counts as a response."""
    now = int(time.time())
    jobs = {
        "j1": {"id": "j1", "company": "Tempus", "sector": "healthtech",
               "source": "greenhouse"}
    }
    applied = [
        {"id": "j1", "applied_at": now - 3 * 86400, "stage": "oa"}
    ]
    result = strategist.response_rates(applied, jobs)
    # OA is a response, so responded count equals applied
    assert result["by_sector"]["healthtech"] == {"applied": 1, "responded": 1}
    assert result["by_source"]["greenhouse"] == {"applied": 1, "responded": 1}


def test_response_rates_interview_counts_as_responded():
    """Interview stage counts as a response."""
    now = int(time.time())
    jobs = {
        "j1": {"id": "j1", "company": "Stripe", "sector": "fintech",
               "source": "ashby"}
    }
    applied = [
        {"id": "j1", "applied_at": now - 3 * 86400, "stage": "interview"}
    ]
    result = strategist.response_rates(applied, jobs)
    assert result["by_sector"]["fintech"] == {"applied": 1, "responded": 1}
    assert result["by_source"]["ashby"] == {"applied": 1, "responded": 1}


def test_response_rates_rejected_counts_as_responded():
    """Rejected stage counts as a response."""
    now = int(time.time())
    jobs = {
        "j1": {"id": "j1", "company": "NVIDIA", "sector": "big_tech",
               "source": "workday"}
    }
    applied = [
        {"id": "j1", "applied_at": now - 3 * 86400, "stage": "rejected"}
    ]
    result = strategist.response_rates(applied, jobs)
    assert result["by_sector"]["big_tech"] == {"applied": 1, "responded": 1}
    assert result["by_source"]["workday"] == {"applied": 1, "responded": 1}


def test_response_rates_older_than_three_weeks_excluded():
    """Applications older than 3 weeks are not counted."""
    now = int(time.time())
    jobs = {
        "j1": {"id": "j1", "company": "OldCo", "sector": "other",
               "source": "unknown"}
    }
    applied = [
        {"id": "j1", "applied_at": now - 4 * 7 * 86400, "stage": "oa"}  # 4 weeks ago
    ]
    result = strategist.response_rates(applied, jobs)
    assert result["by_sector"] == {}
    assert result["by_source"] == {}


def test_response_rates_stage_saved_counts_as_applied():
    """Stage 'saved' (checkbox but not applied) counts as applied, not responded."""
    now = int(time.time())
    jobs = {
        "j1": {"id": "j1", "company": "Tempus", "sector": "healthtech",
               "source": "greenhouse"}
    }
    applied = [
        {"id": "j1", "applied_at": now - 3 * 86400, "stage": "saved"}
    ]
    result = strategist.response_rates(applied, jobs)
    assert result["by_sector"]["healthtech"] == {"applied": 1, "responded": 0}
    assert result["by_source"]["greenhouse"] == {"applied": 1, "responded": 0}


def test_response_rates_combines_multiple_sectors_and_sources():
    """Test aggregation across multiple sectors and sources."""
    now = int(time.time())
    jobs = {
        "j1": {"id": "j1", "company": "Tempus", "sector": "healthtech",
               "source": "greenhouse"},
        "j2": {"id": "j2", "company": "Stripe", "sector": "fintech",
               "source": "ashby"},
        "j3": {"id": "j3", "company": "NVIDIA", "sector": "big_tech",
               "source": "greenhouse"},
        "j4": {"id": "j4", "company": "OpenAI", "sector": "ai_lab",
               "source": "ashby"},
    }
    applied = [
        {"id": "j1", "applied_at": now - 3 * 86400, "stage": "oa"},         # healthtech
        {"id": "j2", "applied_at": now - 2 * 86400, "stage": "interview"},  # fintech
        {"id": "j3", "applied_at": now - 5 * 86400, "stage": "rejected"},   # big_tech
        {"id": "j4", "applied_at": now - 1 * 86400, "stage": "oa"},         # ai_lab
    ]
    result = strategist.response_rates(applied, jobs)

    assert result["by_sector"]["healthtech"] == {"applied": 1, "responded": 1}
    assert result["by_sector"]["fintech"] == {"applied": 1, "responded": 1}
    assert result["by_sector"]["big_tech"] == {"applied": 1, "responded": 1}
    assert result["by_sector"]["ai_lab"] == {"applied": 1, "responded": 1}

    # greenhouse: j1 (oa), j3 (rejected)
    assert result["by_source"]["greenhouse"] == {"applied": 2, "responded": 2}
    # ashby: j2 (interview), j4 (oa)
    assert result["by_source"]["ashby"] == {"applied": 2, "responded": 2}


def test_response_rates_partial_window_exclusion():
    """One entry outside the 3-week window is excluded while others inside remain."""
    now = int(time.time())
    jobs = {
        "j1": {"id": "j1", "company": "Recent Co", "sector": "healthtech", "source": "greenhouse"},
        "j2": {"id": "j2", "company": "Old Co", "sector": "healthtech", "source": "greenhouse"},
    }
    applied = [
        {"id": "j1", "applied_at": now - 5 * 86400, "stage": "oa"},          # within window
        {"id": "j2", "applied_at": now - 4 * 7 * 86400, "stage": "rejected"},  # 4 weeks ago: excluded
    ]
    result = strategist.response_rates(applied, jobs)
    assert result["by_sector"]["healthtech"] == {"applied": 1, "responded": 1}
    assert result["by_source"]["greenhouse"] == {"applied": 1, "responded": 1}


def test_response_rates_missing_job_info_falls_back_to_other():
    """Jobs not in the jobs dict default to 'other' sector and 'unknown' source."""
    now = int(time.time())
    jobs = {}
    applied = [
        {"id": "j1", "applied_at": now - 3 * 86400, "stage": "oa"}
    ]
    result = strategist.response_rates(applied, jobs)
    assert result["by_sector"]["other"] == {"applied": 1, "responded": 1}
    assert result["by_source"]["unknown"] == {"applied": 1, "responded": 1}


def test_response_rates_sample_size_minimum():
    """Only show sectors/sources with at least 2 applications."""
    now = int(time.time())
    jobs = {
        "j1": {"id": "j1", "company": "Tempus", "sector": "healthtech",
               "source": "greenhouse"},
    }
    applied = [
        {"id": "j1", "applied_at": now - 3 * 86400, "stage": "oa"}
    ]
    result = strategist.response_rates(applied, jobs)

    # Single application has stats but sample size of 1
    assert result["by_sector"]["healthtech"] == {"applied": 1, "responded": 1}
    assert result["by_source"]["greenhouse"] == {"applied": 1, "responded": 1}


def test_response_rates_calculation_accuracy():
    """Verify exact percentage calculation."""
    now = int(time.time())
    jobs = {
        "j1": {"id": "j1", "company": "Co1", "sector": "test",
               "source": "src"},
        "j2": {"id": "j2", "company": "Co2", "sector": "test",
               "source": "src"},
        "j3": {"id": "j3", "company": "Co3", "sector": "test",
               "source": "src"},
    }
    applied = [
        {"id": "j1", "applied_at": now - 3 * 86400, "stage": "oa"},      # responded
        {"id": "j2", "applied_at": now - 3 * 86400, "stage": "oa"},      # responded
        {"id": "j3", "applied_at": now - 3 * 86400, "stage": "applied"}, # not responded
    ]
    result = strategist.response_rates(applied, jobs)
    stats = result["by_sector"]["test"]
    assert stats["applied"] == 3
    assert stats["responded"] == 2


def test_response_rates_responded_stages():
    """Verify all response stages (oa, interview, rejected) are counted."""
    now = int(time.time())
    jobs = {
        "j1": {"id": "j1", "company": "Co1", "sector": "test", "source": "src"},
        "j2": {"id": "j2", "company": "Co2", "sector": "test", "source": "src"},
        "j3": {"id": "j3", "company": "Co3", "sector": "test", "source": "src"},
    }
    applied = [
        {"id": "j1", "applied_at": now - 3 * 86400, "stage": "oa"},
        {"id": "j2", "applied_at": now - 3 * 86400, "stage": "interview"},
        {"id": "j3", "applied_at": now - 3 * 86400, "stage": "rejected"},
    ]
    result = strategist.response_rates(applied, jobs)
    stats = result["by_sector"]["test"]
    assert stats["applied"] == 3
    assert stats["responded"] == 3
