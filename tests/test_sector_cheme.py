from radar.sector import infer


def test_seed_sector_wins():
    assert infer("Custom Employer", {"custom employer": "environmental"}) == "environmental"


def test_known_cheme_employers_are_classified():
    expected = {
        "Dow": "chemicals_materials",
        "Chevron": "energy",
        "Gilead Sciences": "pharma_biotech",
        "Micron Technology": "semiconductors",
        "Tesla": "consumer_manufacturing",
        "Veolia": "environmental",
        "Jacobs": "engineering_consulting",
    }
    for company, sector in expected.items():
        assert infer(company, {}) == sector, company


def test_cheme_lexicons_cover_unseeded_employers():
    assert infer("Acme Polymer Materials", {}) == "chemicals_materials"
    assert infer("Northstar Biotech", {}) == "pharma_biotech"
    assert infer("Clearwater Environmental", {}) == "environmental"
