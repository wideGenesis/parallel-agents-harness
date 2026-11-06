import os


def test_project_setup_reaches_h_check():
    # in the harness CI template's own run (template-check), polygon/ci-setup.sh
    # writes POLYGON_SETUP to $GITHUB_ENV before `h check`; this test runs inside h check.
    # Anywhere else there is no setup step and nothing to prove.
    if os.environ.get("GITHUB_WORKFLOW") == "template-check":
        assert os.environ.get("POLYGON_SETUP") == "done"
