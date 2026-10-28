from hedgedoc_mcp.auth import extract_login_form_action

# A trimmed but structurally faithful fragment of Keycloak's default
# "keycloak" login theme (login.ftl), the part that actually matters here:
# the form's id and its HTML-escaped action URL (Keycloak always escapes
# the query string's & as &amp; in the rendered HTML).
KEYCLOAK_DEFAULT_THEME_LOGIN_PAGE = """
<html><body>
<div class="login-pf-page">
  <div id="kc-content">
    <div id="kc-content-wrapper">
      <div id="kc-form">
        <div id="kc-form-wrapper">
          <form id="kc-form-login" onsubmit="login.disabled = true; return true;"
                action="https://keycloak.example.org/realms/hedgedoc/login-actions/authenticate?session_code=abc123&amp;execution=xyz789&amp;client_id=hedgedoc&amp;tab_id=deadbeef"
                method="post">
            <input type="text" id="username" name="username" />
            <input type="password" id="password" name="password" />
          </form>
        </div>
      </div>
    </div>
  </div>
</div>
</body></html>
"""

PAGE_URL = "https://keycloak.example.org/realms/hedgedoc/protocol/openid-connect/auth?client_id=hedgedoc&redirect_uri=..."


def test_extracts_and_unescapes_kc_form_login_action():
    action = extract_login_form_action(KEYCLOAK_DEFAULT_THEME_LOGIN_PAGE, PAGE_URL)
    assert action is not None
    assert action.startswith("https://keycloak.example.org/realms/hedgedoc/login-actions/authenticate?")
    # the whole point of the test: &amp; must become & exactly once, not
    # left escaped and not double-unescaped into something else
    assert "&amp;" not in action
    assert "session_code=abc123&execution=xyz789&client_id=hedgedoc&tab_id=deadbeef" in action


def test_falls_back_to_any_form_when_no_kc_form_login_id():
    generic_html = """
    <html><body>
      <form action="/some/other/login&amp;path" method="post">
        <input name="user" />
      </form>
    </body></html>
    """
    action = extract_login_form_action(generic_html, "https://example.org/login")
    assert action == "https://example.org/some/other/login&path"


def test_returns_none_when_no_form_present():
    # e.g. the session was already authenticated and Keycloak redirected
    # straight back to HedgeDoc without ever showing a login page
    already_logged_in_html = "<html><body>Redirecting...</body></html>"
    assert extract_login_form_action(already_logged_in_html, "https://example.org/") is None


def test_relative_action_is_resolved_against_page_url():
    html_fragment = '<form id="kc-form-login" action="/login-actions/authenticate?x=1">'
    action = extract_login_form_action(html_fragment, "https://keycloak.example.org/realms/cdc/auth")
    assert action == "https://keycloak.example.org/login-actions/authenticate?x=1"
