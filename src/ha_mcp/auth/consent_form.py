"""
Consent form HTML template for Home Assistant OAuth authentication.

This module provides the HTML consent form where users enter their
Long-Lived Access Token (LLAT) to authorize MCP client access.
"""

import html
from urllib.parse import urlparse


def _extract_domain(redirect_uri: str) -> str:
    """Extract display domain from redirect URI."""
    try:
        parsed = urlparse(redirect_uri)
        return parsed.netloc or redirect_uri
    except (AttributeError, TypeError, ValueError):
        return redirect_uri


def create_consent_html(
    client_id: str,
    redirect_uri: str,
    state: str,
    txn_id: str,
    error_message: str | None = None,
) -> str:
    """
    Generate HTML consent form for Home Assistant authentication.

    Args:
        client_id: OAuth client ID
        redirect_uri: OAuth redirect URI (used to derive the display domain)
        state: OAuth state parameter
        txn_id: Transaction ID for this authorization request
        error_message: Optional error message to display

    Returns:
        HTML string for the consent form
    """
    domain = _extract_domain(redirect_uri)
    safe_domain = html.escape(domain)
    safe_client_id = html.escape(client_id)
    safe_redirect_uri = html.escape(redirect_uri)
    safe_state = html.escape(state)
    safe_txn_id = html.escape(txn_id)

    error_html = ""
    if error_message:
        safe_error = html.escape(error_message)
        error_html = f"""
        <div class="error-message">
            <strong>Error:</strong> {safe_error}
        </div>
        """

    return f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Connect to Home Assistant</title>
    <style>
        :root {{
            --primary-color: #03a9f4;
            --primary-hover: #0288d1;
            --error-color: #f44336;
            --error-bg: #ffebee;
            --warning-color: #ff9800;
            --warning-bg: #fff3e0;
            --text-color: #212121;
            --text-secondary: #757575;
            --border-color: #e0e0e0;
            --bg-color: #fafafa;
            --card-bg: #ffffff;
        }}

        @media (prefers-color-scheme: dark) {{
            :root {{
                --primary-color: #29b6f6;
                --primary-hover: #4fc3f7;
                --error-color: #ef5350;
                --error-bg: #3e2723;
                --warning-color: #ffb74d;
                --warning-bg: #2a1f0a;
                --text-color: #e0e0e0;
                --text-secondary: #9e9e9e;
                --border-color: #424242;
                --bg-color: #121212;
                --card-bg: #1e1e1e;
            }}
        }}

        * {{
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }}

        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
            background-color: var(--bg-color);
            color: var(--text-color);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }}

        .container {{
            background: var(--card-bg);
            border-radius: 16px;
            box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1), 0 1px 3px rgba(0, 0, 0, 0.08);
            max-width: 440px;
            width: 100%;
            padding: 32px;
        }}

        .header {{
            text-align: center;
            margin-bottom: 24px;
        }}

        .logo {{
            width: 80px;
            height: 80px;
            margin-bottom: 16px;
        }}

        h1 {{
            font-size: 24px;
            font-weight: 600;
            margin-bottom: 8px;
        }}

        .subtitle {{
            color: var(--text-secondary);
            font-size: 14px;
        }}

        .error-message {{
            background: var(--error-bg);
            border: 1px solid var(--error-color);
            border-radius: 8px;
            padding: 12px 16px;
            margin-bottom: 20px;
            font-size: 14px;
            color: var(--error-color);
        }}

        .warning-box {{
            background: var(--warning-bg);
            border: 1px solid var(--warning-color);
            border-radius: 8px;
            padding: 12px 16px;
            margin-bottom: 20px;
            font-size: 13px;
            color: var(--text-color);
            line-height: 1.5;
        }}

        .warning-box strong {{
            color: var(--warning-color);
        }}

        .form-group {{
            margin-bottom: 20px;
        }}

        label {{
            display: block;
            font-size: 14px;
            font-weight: 500;
            margin-bottom: 8px;
        }}

        input[type="password"] {{
            width: 100%;
            padding: 12px 16px;
            font-size: 16px;
            border: 1px solid var(--border-color);
            border-radius: 8px;
            background: var(--card-bg);
            color: var(--text-color);
            transition: border-color 0.2s, box-shadow 0.2s;
        }}

        input:focus {{
            outline: none;
            border-color: var(--primary-color);
            box-shadow: 0 0 0 3px rgba(3, 169, 244, 0.1);
        }}

        input::placeholder {{
            color: var(--text-secondary);
        }}

        .help-text {{
            font-size: 12px;
            color: var(--text-secondary);
            margin-top: 6px;
        }}

        .help-text a {{
            color: var(--primary-color);
            text-decoration: none;
        }}

        .help-text a:hover {{
            text-decoration: underline;
        }}

        .button-group {{
            display: flex;
            gap: 12px;
            margin-top: 24px;
        }}

        button {{
            flex: 1;
            padding: 14px 24px;
            font-size: 16px;
            font-weight: 500;
            border-radius: 8px;
            cursor: pointer;
            transition: all 0.2s;
        }}

        .btn-primary {{
            background: var(--primary-color);
            color: white;
            border: none;
        }}

        .btn-primary:hover {{
            background: var(--primary-hover);
        }}

        .btn-primary:disabled {{
            background: var(--border-color);
            cursor: not-allowed;
        }}

        .btn-secondary {{
            background: transparent;
            color: var(--text-color);
            border: 1px solid var(--border-color);
        }}

        .btn-secondary:hover {{
            background: var(--bg-color);
        }}

        .loading {{
            display: none;
        }}

        .loading.active {{
            display: inline-block;
        }}

        @keyframes spin {{
            to {{ transform: rotate(360deg); }}
        }}

        .spinner {{
            width: 16px;
            height: 16px;
            border: 2px solid transparent;
            border-top-color: currentColor;
            border-radius: 50%;
            animation: spin 0.8s linear infinite;
            display: inline-block;
            vertical-align: middle;
            margin-right: 8px;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <svg class="logo" viewBox="0 0 240 240" xmlns="http://www.w3.org/2000/svg">
                <path fill="#18BCF2" d="M120 0C53.7 0 0 53.7 0 120s53.7 120 120 120 120-53.7 120-120S186.3 0 120 0zm0 220c-55.2 0-100-44.8-100-100S64.8 20 120 20s100 44.8 100 100-44.8 100-100 100z"/>
                <path fill="#18BCF2" d="M120 40c-44.1 0-80 35.9-80 80s35.9 80 80 80 80-35.9 80-80-35.9-80-80-80zm0 140c-33.1 0-60-26.9-60-60s26.9-60 60-60 60 26.9 60 60-26.9 60-60 60z"/>
                <circle fill="#18BCF2" cx="120" cy="120" r="40"/>
            </svg>
            <h1>Connect to Home Assistant</h1>
            <p class="subtitle">Authorization request from <strong>{safe_domain}</strong></p>
        </div>

        {error_html}

        <div class="warning-box">
            <strong>Important:</strong> Your access token will be shared with
            <strong>{safe_domain}</strong> and used for ongoing access to your
            Home Assistant instance. To revoke access, delete the token in
            Home Assistant &rarr; Profile &rarr; Security &rarr; Long-Lived Access Tokens.
        </div>

        <form method="POST" id="consent-form">
            <input type="hidden" name="txn_id" value="{safe_txn_id}">
            <input type="hidden" name="client_id" value="{safe_client_id}">
            <input type="hidden" name="redirect_uri" value="{safe_redirect_uri}">
            <input type="hidden" name="state" value="{safe_state}">

            <div class="form-group">
                <label for="ha_token">Long-Lived Access Token</label>
                <input
                    type="password"
                    id="ha_token"
                    name="ha_token"
                    placeholder="Enter your access token"
                    required
                    autocomplete="off"
                >
                <p class="help-text">
                    Create a token at: Home Assistant &rarr; Profile &rarr;
                    <a href="https://www.home-assistant.io/docs/authentication/#your-account-profile" target="_blank" rel="noopener">
                        Long-Lived Access Tokens
                    </a>
                </p>
            </div>

            <div class="button-group">
                <button type="button" class="btn-secondary" onclick="window.close(); history.back();">
                    Cancel
                </button>
                <button type="submit" class="btn-primary" id="submit-btn">
                    <span class="loading" id="loading">
                        <span class="spinner"></span>
                    </span>
                    <span id="btn-text">Authorize</span>
                </button>
            </div>
        </form>
    </div>

    <script>
        document.getElementById('consent-form').addEventListener('submit', function(e) {{
            var btn = document.getElementById('submit-btn');
            var loading = document.getElementById('loading');
            var btnText = document.getElementById('btn-text');

            btn.disabled = true;
            loading.classList.add('active');
            btnText.textContent = 'Authorizing...';
        }});
    </script>
</body>
</html>
"""


def create_error_html(error: str, error_description: str) -> str:
    """
    Generate HTML error page for OAuth errors.

    Args:
        error: OAuth error code
        error_description: Human-readable error description

    Returns:
        HTML string for the error page
    """
    safe_error = html.escape(error)
    safe_description = html.escape(error_description)

    return f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Authentication Error</title>
    <style>
        :root {{
            --error-color: #f44336;
            --text-color: #212121;
            --text-secondary: #757575;
            --bg-color: #fafafa;
            --card-bg: #ffffff;
        }}

        @media (prefers-color-scheme: dark) {{
            :root {{
                --error-color: #ef5350;
                --text-color: #e0e0e0;
                --text-secondary: #9e9e9e;
                --bg-color: #121212;
                --card-bg: #1e1e1e;
            }}
        }}

        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background-color: var(--bg-color);
            color: var(--text-color);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 20px;
            margin: 0;
        }}

        .container {{
            background: var(--card-bg);
            border-radius: 16px;
            box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
            max-width: 440px;
            width: 100%;
            padding: 32px;
            text-align: center;
        }}

        .error-icon {{
            width: 64px;
            height: 64px;
            margin-bottom: 16px;
            color: var(--error-color);
        }}

        h1 {{
            font-size: 24px;
            font-weight: 600;
            margin-bottom: 8px;
            color: var(--error-color);
        }}

        .error-code {{
            font-family: monospace;
            background: var(--bg-color);
            padding: 4px 8px;
            border-radius: 4px;
            font-size: 14px;
            margin-bottom: 16px;
            display: inline-block;
        }}

        p {{
            color: var(--text-secondary);
            font-size: 14px;
            line-height: 1.6;
        }}
    </style>
</head>
<body>
    <div class="container">
        <svg class="error-icon" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor">
            <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-2h2v2zm0-4h-2V7h2v6z"/>
        </svg>
        <h1>Authentication Error</h1>
        <div class="error-code">{safe_error}</div>
        <p>{safe_description}</p>
    </div>
</body>
</html>
"""
