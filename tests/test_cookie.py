from __future__ import annotations

import importlib
import sys
import types
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def cookie_module(monkeypatch: pytest.MonkeyPatch):
    logger = SimpleNamespace(
        debug=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None
    )

    astrbot_pkg = types.ModuleType("astrbot")
    astrbot_pkg.__path__ = []
    api_module = types.ModuleType("astrbot.api")
    api_module.logger = logger
    monkeypatch.setitem(sys.modules, "astrbot", astrbot_pkg)
    monkeypatch.setitem(sys.modules, "astrbot.api", api_module)

    config_module = types.ModuleType("core.config")
    config_module.ParserItem = object
    config_module.PluginConfig = object
    monkeypatch.setitem(sys.modules, "core.config", config_module)

    monkeypatch.delitem(sys.modules, "core.cookie", raising=False)

    return importlib.import_module("core.cookie")


def build_cookie_jar(
    cookie_module,
    tmp_path: Path,
    raw_cookies: str,
    *,
    domain: str = "instagram.com",
    parser_name: str = "instagram",
):
    config = SimpleNamespace(cookie_dir=tmp_path)
    parser_cfg = SimpleNamespace(name=parser_name, cookies=raw_cookies)
    return cookie_module.CookieJar(config, parser_cfg, domain=domain)


def load_cookie_file_entries(cookie_file: Path) -> dict[str, dict[str, object]]:
    jar = MozillaCookieJar(cookie_file)
    jar.load(ignore_discard=True, ignore_expires=True)
    return {
        cookie.name: {
            "value": cookie.value or "",
            "domain": cookie.domain,
            "path": cookie.path,
            "secure": cookie.secure,
            "expires": cookie.expires,
        }
        for cookie in jar
    }


def load_cookie_file_rows(cookie_file: Path) -> list[dict[str, object]]:
    jar = MozillaCookieJar(cookie_file)
    jar.load(ignore_discard=True, ignore_expires=True)
    return [
        {
            "name": cookie.name,
            "value": cookie.value or "",
            "domain": cookie.domain,
            "path": cookie.path,
            "secure": cookie.secure,
            "expires": cookie.expires,
        }
        for cookie in jar
    ]


def load_cookie_file(cookie_file: Path) -> dict[str, str]:
    return {
        name: str(entry["value"])
        for name, entry in load_cookie_file_entries(cookie_file).items()
    }


def test_header_cookie_string_input_still_works(cookie_module, tmp_path: Path):
    jar = build_cookie_jar(
        cookie_module,
        tmp_path,
        "sessionid=abc123; ds_user_id=42; csrftoken=token-value",
    )

    assert jar.get() == {
        "sessionid": "abc123",
        "ds_user_id": "42",
        "csrftoken": "token-value",
    }
    assert load_cookie_file(jar.cookie_file) == {
        "sessionid": "abc123",
        "ds_user_id": "42",
        "csrftoken": "token-value",
    }


def test_header_cookie_string_with_tabs_and_newlines_still_uses_header_parsing(
    cookie_module, tmp_path: Path
):
    jar = build_cookie_jar(
        cookie_module,
        tmp_path,
        "sessionid=abc123;\n\tds_user_id=42;\tcsrftoken=token-value",
    )

    assert jar.get() == {
        "sessionid": "abc123",
        "ds_user_id": "42",
        "csrftoken": "token-value",
    }
    assert load_cookie_file(jar.cookie_file) == {
        "sessionid": "abc123",
        "ds_user_id": "42",
        "csrftoken": "token-value",
    }


def test_header_cookie_value_containing_netscape_phrase_still_uses_header_parsing(
    cookie_module, tmp_path: Path
):
    jar = build_cookie_jar(
        cookie_module,
        tmp_path,
        "foo=netscape http cookie file; bar=baz",
        parser_name="instagram_header_phrase",
    )

    assert jar.get() == {
        "foo": "netscape http cookie file",
        "bar": "baz",
    }
    assert load_cookie_file(jar.cookie_file) == {
        "foo": "netscape http cookie file",
        "bar": "baz",
    }


def test_single_netscape_like_header_line_still_uses_header_parsing(
    cookie_module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(cookie_module.CookieJar, "save_to_file", lambda self: None)

    jar = build_cookie_jar(
        cookie_module,
        tmp_path,
        "foo=.instagram.com\tTRUE\t/\tTRUE\t2147483647\tsessionid\tabc123; bar=baz",
        parser_name="instagram_single_row_header",
    )

    assert jar.get() == {
        "foo": ".instagram.com\tTRUE\t/\tTRUE\t2147483647\tsessionid\tabc123",
        "bar": "baz",
    }


def test_two_netscape_like_header_lines_still_use_header_parsing(
    cookie_module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(cookie_module.CookieJar, "save_to_file", lambda self: None)

    jar = build_cookie_jar(
        cookie_module,
        tmp_path,
        "foo=.instagram.com\tTRUE\t/\tTRUE\t2147483647\talpha\tone;\nbar=.instagram.com\tTRUE\t/\tTRUE\t2147483647\tbeta\ttwo",
        parser_name="instagram_two_row_header",
    )

    assert jar.get() == {
        "foo": ".instagram.com\tTRUE\t/\tTRUE\t2147483647\talpha\tone",
        "bar": ".instagram.com\tTRUE\t/\tTRUE\t2147483647\tbeta\ttwo",
    }


def test_netscape_cookie_file_input_parses_and_writes_runtime_file(
    cookie_module, tmp_path: Path
):
    jar = build_cookie_jar(
        cookie_module,
        tmp_path,
        """# Netscape HTTP Cookie File
.instagram.com\tTRUE\t/\tTRUE\t2147483647\tsessionid\tabc123
.instagram.com\tTRUE\t/\tTRUE\t2147483647\tds_user_id\t42
.instagram.com\tTRUE\t/\tTRUE\t2147483647\tcsrftoken\ttoken-value
""",
    )

    assert jar.get() == {
        "sessionid": "abc123",
        "ds_user_id": "42",
        "csrftoken": "token-value",
    }
    assert load_cookie_file(jar.cookie_file) == {
        "sessionid": "abc123",
        "ds_user_id": "42",
        "csrftoken": "token-value",
    }

    persisted = load_cookie_file_entries(jar.cookie_file)
    assert persisted["sessionid"] == {
        "value": "abc123",
        "domain": ".instagram.com",
        "path": "/",
        "secure": True,
        "expires": 2147483647,
    }


def test_netscape_duplicate_cookie_names_are_preserved_in_runtime_file_and_header(
    cookie_module, tmp_path: Path
):
    jar = build_cookie_jar(
        cookie_module,
        tmp_path,
        """# Netscape HTTP Cookie File
.instagram.com\tTRUE\t/\tTRUE\t2147483647\tsessionid\troot-value
.instagram.com\tTRUE\t/api\tTRUE\t2147483647\tsessionid\tapi-value
""",
        parser_name="instagram_duplicate_names",
    )

    rows = load_cookie_file_rows(jar.cookie_file)
    sessionid_rows = [row for row in rows if row["name"] == "sessionid"]

    assert len(sessionid_rows) == 2
    assert {row["path"] for row in sessionid_rows} == {"/", "/api"}

    header = jar.get_cookie_header_for_url("https://www.instagram.com/api")
    assert header.count("sessionid=") == 2
    assert "sessionid=root-value" in header
    assert "sessionid=api-value" in header
    assert header.index("sessionid=api-value") < header.index("sessionid=root-value")


def test_netscape_get_prefers_most_specific_duplicate_cookie(
    cookie_module, tmp_path: Path
):
    jar = build_cookie_jar(
        cookie_module,
        tmp_path,
        """# Netscape HTTP Cookie File
.instagram.com\tTRUE\t/api\tTRUE\t2147483647\tsessionid\tapi-value
.instagram.com\tTRUE\t/\tTRUE\t2147483647\tsessionid\troot-value
""",
        parser_name="instagram_duplicate_get",
    )

    assert jar.get(path="/api") == {"sessionid": "api-value"}
    assert jar.get(path="/api/v1") == {"sessionid": "api-value"}


def test_to_dict_uses_default_lookup_semantics_for_duplicate_cookie_names(
    cookie_module, tmp_path: Path
):
    jar = build_cookie_jar(
        cookie_module,
        tmp_path,
        """# Netscape HTTP Cookie File
.instagram.com\tTRUE\t/\tTRUE\t2147483647\tsessionid\troot-value
.instagram.com\tTRUE\t/api\tTRUE\t2147483647\tsessionid\tapi-value
""",
        parser_name="instagram_to_dict_duplicates",
    )

    assert jar.to_dict() == {"sessionid": "root-value"}


def test_netscape_comments_blank_and_malformed_lines_are_ignored_safely(
    cookie_module, tmp_path: Path
):
    jar = build_cookie_jar(
        cookie_module,
        tmp_path,
        """# Netscape HTTP Cookie File

# a comment
not\ta\tvalid\tline
.instagram.com\tTRUE\t/\tTRUE\t2147483647\tsessionid\tabc123
#HttpOnly_.instagram.com\tTRUE\t/\tTRUE\tnot-a-timestamp\tcsrftoken\tbad-value
.instagram.com\tTRUE\t/\tFALSE\t2147483647\tcsrftoken\ttoken-value
""",
    )

    assert jar.get() == {
        "sessionid": "abc123",
        "csrftoken": "token-value",
    }
    assert load_cookie_file(jar.cookie_file) == {
        "sessionid": "abc123",
        "csrftoken": "token-value",
    }


def test_explicit_netscape_marker_without_valid_rows_is_not_reparsed_as_header(
    cookie_module, tmp_path: Path
):
    jar = build_cookie_jar(
        cookie_module,
        tmp_path,
        """# Netscape HTTP Cookie File
# comment only
sessionid=abc123
""",
        parser_name="instagram_marker_only",
    )

    assert jar.get() == {}
    assert load_cookie_file(jar.cookie_file) == {}


def test_netscape_cookie_path_requires_directory_boundary(
    cookie_module, tmp_path: Path
):
    jar = build_cookie_jar(
        cookie_module,
        tmp_path,
        """# Netscape HTTP Cookie File
.instagram.com\tTRUE\t/foo\tTRUE\t2147483647\tcsrftoken\ttoken-value
""",
        parser_name="instagram_path_scope",
    )

    assert jar.get(path="/foo") == {"csrftoken": "token-value"}
    assert jar.get(path="/foo/") == {"csrftoken": "token-value"}
    assert jar.get(path="/foo/bar") == {"csrftoken": "token-value"}
    assert jar.get(path="/foobar") == {}

    root_jar = build_cookie_jar(
        cookie_module,
        tmp_path,
        """# Netscape HTTP Cookie File
.instagram.com\tTRUE\t/\tTRUE\t2147483647\trootid\troot-value
""",
        parser_name="instagram_root_path_scope",
    )

    assert root_jar.get(path="/") == {"rootid": "root-value"}
    assert root_jar.get(path="/foo") == {"rootid": "root-value"}
    assert root_jar.get(path="/foo/bar") == {"rootid": "root-value"}
    assert root_jar.get(path="") == {}

    nested_jar = build_cookie_jar(
        cookie_module,
        tmp_path,
        """# Netscape HTTP Cookie File
.instagram.com\tTRUE\t/foo/bar\tTRUE\t2147483647\tbarid\tbar-value
""",
        parser_name="instagram_nested_path_scope",
    )

    assert nested_jar.get(path="/foo/bar") == {"barid": "bar-value"}
    assert nested_jar.get(path="/foo/bar/") == {"barid": "bar-value"}
    assert nested_jar.get(path="/foo/bar/baz") == {"barid": "bar-value"}
    assert nested_jar.get(path="/foo/barbaz") == {}
    assert nested_jar.get(path="/foo/barista") == {}


def test_netscape_cookie_secure_flag_behavior(cookie_module, tmp_path: Path):
    jar = build_cookie_jar(
        cookie_module,
        tmp_path,
        """# Netscape HTTP Cookie File
.example.com\tTRUE\t/foo\tTRUE\t2147483647\tsecureid\tsecure-value
.example.com\tTRUE\t/foo\tFALSE\t2147483647\tnonsecureid\tnonsecure-value
""",
        domain="example.com",
        parser_name="example_secure_scope",
    )

    non_secure_header = jar.get_cookie_header_for_url("http://example.com/foo")
    assert "nonsecureid=nonsecure-value" in non_secure_header
    assert "secureid=secure-value" not in non_secure_header

    secure_header = jar.get_cookie_header_for_url("https://example.com/foo")
    assert "nonsecureid=nonsecure-value" in secure_header
    assert "secureid=secure-value" in secure_header


def test_netscape_purge_expired_cookies_removes_expired_and_persists_valid(
    cookie_module, tmp_path: Path
):
    jar = build_cookie_jar(
        cookie_module,
        tmp_path,
        """# Netscape HTTP Cookie File
.instagram.com\tTRUE\t/\tFALSE\t1\texpired_cookie\told-value
.instagram.com\tTRUE\t/\tFALSE\t2147483647\tvalid_cookie\tnew-value
""",
        parser_name="instagram_purge_expired",
    )

    assert jar.get() == {"valid_cookie": "new-value"}
    assert len(load_cookie_file_rows(jar.cookie_file)) == 2

    jar.purge_expired()

    assert jar.get() == {"valid_cookie": "new-value"}
    assert jar.get_cookie_header_for_url("https://www.instagram.com/") == (
        "valid_cookie=new-value"
    )
    assert load_cookie_file(jar.cookie_file) == {"valid_cookie": "new-value"}


def test_netscape_cookie_with_false_subdomains_stays_exact_host(
    cookie_module, tmp_path: Path
):
    raw_cookies = """# Netscape HTTP Cookie File
.instagram.com\tFALSE\t/\tTRUE\t2147483647\tsessionid\tabc123
"""

    exact_host_jar = build_cookie_jar(
        cookie_module,
        tmp_path,
        raw_cookies,
        domain="instagram.com",
        parser_name="instagram_exact_host",
    )
    subdomain_jar = build_cookie_jar(
        cookie_module,
        tmp_path,
        raw_cookies,
        domain="www.instagram.com",
        parser_name="instagram_subdomain_host",
    )

    assert exact_host_jar.get() == {"sessionid": "abc123"}
    assert load_cookie_file_entries(exact_host_jar.cookie_file)["sessionid"][
        "domain"
    ] == ("instagram.com")
    assert subdomain_jar.get() == {}


def test_netscape_domain_cookie_requires_host_boundary(cookie_module, tmp_path: Path):
    raw_cookies = """# Netscape HTTP Cookie File
.instagram.com\tTRUE\t/\tTRUE\t2147483647\tsessionid\tabc123
"""

    matching_jar = build_cookie_jar(
        cookie_module,
        tmp_path,
        raw_cookies,
        domain="www.instagram.com",
        parser_name="instagram_domain_boundary_match",
    )
    unrelated_jar = build_cookie_jar(
        cookie_module,
        tmp_path,
        raw_cookies,
        domain="badinstagram.com",
        parser_name="instagram_domain_boundary_miss",
    )

    assert matching_jar.get() == {"sessionid": "abc123"}
    assert unrelated_jar.get() == {}


def test_get_cookie_header_for_url_uses_url_hostname(cookie_module, tmp_path: Path):
    jar = build_cookie_jar(
        cookie_module,
        tmp_path,
        "sessionid=abc123; csrftoken=token-value",
        parser_name="instagram_url_hostname",
    )

    assert jar.get_cookie_header_for_url("https://www.instagram.com/foo") == (
        "sessionid=abc123; csrftoken=token-value"
    )
    assert jar.get_cookie_header_for_url("https://badinstagram.com/foo") == ""


def test_get_and_cookie_header_respect_domain_override_mismatch(
    cookie_module, tmp_path: Path
):
    jar = build_cookie_jar(
        cookie_module,
        tmp_path,
        "sessionid=abc123; csrftoken=token-value",
        domain="instagram.com",
        parser_name="instagram_domain_override_mismatch",
    )

    assert jar.get(domain="badinstagram.com") == {}
    assert jar.get_cookie_header(path="/foo", domain="badinstagram.com") == ""


def test_get_and_cookie_header_respect_domain_override_match(
    cookie_module, tmp_path: Path
):
    jar = build_cookie_jar(
        cookie_module,
        tmp_path,
        """# Netscape HTTP Cookie File
.instagram.com\tTRUE\t/\tTRUE\t2147483647\tsessionid\tabc123
.instagram.com\tTRUE\t/\tTRUE\t2147483647\tcsrftoken\ttoken-value
""",
        domain="badinstagram.com",
        parser_name="instagram_domain_override_match",
    )

    assert jar.get(domain="instagram.com") == {
        "sessionid": "abc123",
        "csrftoken": "token-value",
    }
    assert jar.get_cookie_header(path="/foo", domain="instagram.com") == (
        "sessionid=abc123; csrftoken=token-value"
    )


def test_netscape_cookie_file_preserves_empty_cookie_values(
    cookie_module, tmp_path: Path
):
    jar = build_cookie_jar(
        cookie_module,
        tmp_path,
        """# Netscape HTTP Cookie File
.instagram.com\tTRUE\t/\tTRUE\t2147483647\tmid\t
""",
    )

    assert jar.get() == {"mid": ""}
    assert load_cookie_file(jar.cookie_file) == {"mid": ""}
