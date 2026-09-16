"""Source-adapter tests that need no network.

URL construction is worth testing on its own: the roll PDF path was reverse-read
from the portal's JavaScript, and a silent change there would send the downloader
to a 404 (or, worse, to the wrong part).
"""
from app.sources.ceo_uttarakhand.client import (
    Form20Client, LegacyRoll2003Client, SirPartsClient,
)
from app.sources.eci_api.client import BASE, EciApiClient


class TestEciUrls:
    def test_endpoint_shapes(self):
        api = EciApiClient(http=None)
        assert api.states_url() == f"{BASE}/common/states"
        assert api.districts_url("S28") == f"{BASE}/common/districts/S28"
        assert api.acs_url("S2813") == f"{BASE}/common/acs/S2813"

    def test_state_code_is_never_hard_coded_in_the_adapter(self):
        import inspect

        import app.sources.eci_api.client as mod
        src = inspect.getsource(mod)
        # "S28" may appear in a docstring/example but must not be a literal the
        # code depends on; find_state resolves it from the live list.
        assert "find_state" in src
        code_lines = [l for l in src.splitlines()
                      if '"S28"' in l and not l.strip().startswith("#")]
        assert not code_lines, f"hard-coded state code found: {code_lines}"


class TestCeoUrls:
    def test_sir_parts_url(self):
        c = SirPartsClient(http=None)
        assert c.parts_url(19).endswith("/asdlist/SearchAdsEpic/Parts?acNo=19")
        assert c.ps_list_pdf_url(19).endswith("/PSSIR2026/19.pdf")

    def test_legacy_roll_path_matches_the_portal_convention(self):
        # files/Roll2003/{ac:02d}-{acName}/P{ac:03d}{part:04d}.pdf
        p = LegacyRoll2003Client.roll_relative_path(15, "राजपुर", 6)
        assert p == "files/Roll2003/15-राजपुर/P0150006.pdf"
        assert LegacyRoll2003Client.roll_relative_path(1, "पुरोला", 123) == \
            "files/Roll2003/01-पुरोला/P0010123.pdf"

    def test_legacy_roll_url_is_percent_encoded(self):
        c = LegacyRoll2003Client(http=None)
        url = c.roll_pdf_url(15, "राजपुर", 6)
        assert "filePath=files%2FRoll2003%2F" in url
        assert " " not in url

    def test_form20_years_are_declared_not_guessed(self):
        c = Form20Client(http=None)
        assert "VidhanSabha2022" in c.index_url(2022)
        assert "Vidhan_sabha2012" in c.index_url(2012)
        try:
            c.index_url(1999)
        except LookupError as exc:
            assert "known years" in str(exc)
        else:
            raise AssertionError("an unknown year must raise, not invent a URL")


class TestNoCaptchaEndpointsAreImplemented:
    """The adapters must not contain the gated operations, even accidentally."""

    def test_captcha_gated_operations_are_absent(self):
        import inspect

        import app.sources.ceo_uttarakhand.client as mod
        src = inspect.getsource(mod)
        for forbidden in ("SearchAdsEpic/Captcha", "DownloadAsdPdf",
                          "DownloadBlaMinutes", "getCaptcha"):
            callable_use = [
                l for l in src.splitlines()
                if forbidden in l and "http" in l.lower()
                and not l.strip().startswith("#")
                and not l.strip().startswith(">")
            ]
            assert not callable_use, f"{forbidden} appears to be called: {callable_use}"

    def test_per_elector_search_is_not_wrapped(self):
        c = LegacyRoll2003Client(http=None)
        for name in ("search", "search_electors", "epic_no", "export"):
            assert not hasattr(c, name), (
                f"{name} must stay unimplemented: enumerating electors through the "
                "search API is exactly the brute-force pattern the brief forbids")
