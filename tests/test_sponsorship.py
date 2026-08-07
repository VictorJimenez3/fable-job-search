import zipfile

from radar import sponsorship


def _inline_cell(reference, value):
    return '<c r="%s" t="inlineStr"><is><t>%s</t></is></c>' % (reference, value)


def _make_workbook(path):
    headers = {
        "B1": "CASE_STATUS",
        "D1": "DECISION_DATE",
        "F1": "VISA_CLASS",
        "M1": "TOTAL_WORKER_POSITIONS",
        "T1": "EMPLOYER_NAME",
        "U1": "TRADE_NAME_DBA",
    }
    rows = [
        (2, {"B": "Certified", "D": "46000", "F": "H-1B", "M": "3",
             "T": "NVIDIA Corporation", "U": "NVIDIA"}),
        (3, {"B": "Certified - Withdrawn", "D": "46001", "F": "E-3", "M": "2",
             "T": "NVIDIA Corporation", "U": "NVIDIA"}),
        (4, {"B": "Certified", "D": "46002", "F": "H-2B", "M": "9",
             "T": "NVIDIA Corporation", "U": "NVIDIA"}),
        (5, {"B": "Denied", "D": "46003", "F": "H-1B", "M": "4",
             "T": "NVIDIA Corporation", "U": "NVIDIA"}),
    ]
    header_xml = "".join(_inline_cell(ref, value) for ref, value in headers.items())
    row_xml = ['<row r="1">%s</row>' % header_xml]
    for row_number, values in rows:
        cells = "".join(_inline_cell(column + str(row_number), value)
                         for column, value in values.items())
        row_xml.append('<row r="%s">%s</row>' % (row_number, cells))
    sheet = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData>%s</sheetData></worksheet>' % "".join(row_xml)
    )
    shared = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="0" uniqueCount="0"/>'
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/sharedStrings.xml", shared)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)


def test_company_key_removes_legal_noise_without_fuzzy_matching():
    assert sponsorship.company_key("NVIDIA Corporation") == "nvidia"
    assert sponsorship.company_key("Johnson & Johnson Services, Inc.") == "johnson johnson"
    assert sponsorship.company_key("Mayo Clinic Rochester") == "mayo clinic rochester"


def test_read_workbook_aggregates_certified_lca_history(tmp_path):
    path = tmp_path / "lca.xlsx"
    _make_workbook(path)
    companies = {}

    rows = sponsorship._read_workbook(path, "FY2026 Q2", companies)

    assert rows == 4
    record = companies["nvidia"]
    assert record["filings"] == 2
    assert record["certified_cases"] == 1
    assert record["certified_withdrawn_cases"] == 1
    assert record["certified_workers"] == 3
    assert record["certified_withdrawn_workers"] == 2
    assert record["quarters"] == ["FY2026 Q2"]
    assert record["latest_decision_date"]


def test_discover_files_keeps_latest_main_disclosures_only():
    class Response:
        text = """
        <a href="/media/LCA_Dislclosure_Data_FY2026_Q2.xlsx">current</a>
        <a href="/sites/dolgov/files/ETA/oflc/pdfs/LCA_Disclosure_Data_FY2025_Q4.xlsx">prior</a>
        <a href="/media/LCA_Disclosure_Data_FY2026_Q2_Appendix.xlsx">appendix</a>
        <a href="/media/LCA_Disclosure_Data_FY2026_Q2_Worksites.xlsx">worksites</a>
        """

        def raise_for_status(self):
            return None

    class Session:
        def get(self, url, timeout):
            assert url == sponsorship.DOL_PERFORMANCE_URL
            return Response()

    files = sponsorship.discover_files(Session())

    assert [item["label"] for item in files] == ["FY2026 Q2", "FY2025 Q4"]
    assert files[0]["url"].endswith("LCA_Dislclosure_Data_FY2026_Q2.xlsx")


def test_history_matching_is_conservative_and_explained():
    database = sponsorship.build_alias_index({
        "coverage_quarters": ["FY2026 Q2"],
        "source_url": sponsorship.DOL_PERFORMANCE_URL,
        "companies": {
            "nvidia": {
                "company_key": "nvidia", "display_name": "NVIDIA Corporation",
                "aliases": ["NVIDIA"], "filings": 7, "certified_cases": 6,
                "certified_withdrawn_cases": 1, "certified_workers": 8,
                "certified_withdrawn_workers": 1, "quarters": ["FY2026 Q2"],
                "latest_decision_date": "2026-01-01", "status": "likely",
            },
            "mayo clinic": {
                "company_key": "mayo clinic", "display_name": "Mayo Clinic",
                "aliases": [], "filings": 2, "certified_cases": 2,
                "certified_withdrawn_cases": 0, "certified_workers": 2,
                "certified_withdrawn_workers": 0, "quarters": ["FY2026 Q2"],
                "latest_decision_date": "2026-01-02", "status": "likely",
            },
        },
    })

    assert sponsorship.history_for("NVIDIA Corporation", database)["status"] == "likely"
    assert sponsorship.history_for("Mayo Clinic Rochester", database)["status"] == "likely"
    assert sponsorship.history_for("Acme Robotics", database)["status"] == "no-history"

    record = {"company": "NVIDIA", "score": 91, "score_reasons": ["base utility: 40"]}
    sponsorship.annotate_record(record, database)
    assert record["score"] == 91
    assert record["sponsorship_history"]["certified_cases"] == 6
    assert record["score_reasons"][-1].startswith("sponsor history: likely")


def test_empty_database_is_explicitly_unavailable():
    record = {"company": "NVIDIA", "score": 80, "score_reasons": []}

    sponsorship.annotate_record(record, {})

    assert record["sponsorship_history"]["status"] == "unavailable"
    assert "sponsor history: unavailable" in record["score_reasons"][-1]
