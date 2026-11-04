from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
INSTALL = (ROOT / "scripts" / "mac-companion" / "install.sh").read_text()
RUN = (ROOT / "scripts" / "mac-companion" / "run.sh").read_text()


def test_companion_defaults_to_a_python312_environment_and_rejects_old_venvs():
    assert 'VENV_DIR="${JOBRADAR_VENV:-$BASE/venv312}"' in INSTALL
    assert 'VENV_DIR="${JOBRADAR_VENV:-$HOME/.jobradar/venv312}"' in RUN
    assert "sys.version_info >= (3, 12)" in INSTALL
    assert "sys.version_info >= (3, 12)" in RUN
    assert 'python3 -m venv "$VENV_DIR"' not in INSTALL
    assert 'python3 -m venv "$VENV_DIR"' not in RUN
    assert "older than 3.12" in INSTALL
    assert "older than 3.12" in RUN


def test_social_launchd_environment_preserves_opencli_and_optional_profile():
    assert 'OPENCLI_BIN="$(command -v opencli || true)"' in INSTALL
    assert 'NODE_BIN="$(command -v node || true)"' in INSTALL
    assert 'OPENCLI_PROFILE_XML="$(xml_escape "${OPENCLI_PROFILE:-}")"' in INSTALL
    assert '<key>OPENCLI_BIN</key><string>$OPENCLI_BIN_XML</string>' in INSTALL
    assert '<key>OPENCLI_PROFILE</key><string>$OPENCLI_PROFILE_XML</string>' in INSTALL
    assert '<key>JOBRADAR_VENV</key><string>$VENV_DIR_XML</string>' in INSTALL
    assert '<key>PATH</key><string>$SOCIAL_PATH_XML</string>' in INSTALL
    assert "xml_escape()" in INSTALL


def test_enrichment_sync_fails_closed_on_local_commit_inspection_error():
    assert 'if ! ahead_paths="$(git diff --name-only "origin/$BRANCH..HEAD"' in RUN
    assert "could not inspect local commits" in RUN
