"""
Consent form HTML template for Home Assistant OAuth authentication.

This module provides the HTML consent form where users enter their
Home Assistant URL and Long-Lived Access Token (LLAT).
"""


def create_consent_html(
    client_id: str,
    client_name: str | None,
    redirect_uri: str,
    state: str,
    scopes: list[str],
    error_message: str | None = None,
) -> str:
    """
    Generate HTML consent form for Home Assistant authentication.

    Args:
        client_id: OAuth client ID
        client_name: Human-readable client name
        redirect_uri: OAuth redirect URI
        state: OAuth state parameter
        scopes: Requested OAuth scopes
        error_message: Optional error message to display

    Returns:
        HTML string for the consent form
    """
    display_name = client_name or client_id
    scopes_display = ", ".join(scopes) if scopes else "full access"

    error_html = ""
    if error_message:
        error_html = f"""
        <div class="error-message">
            <strong>Error:</strong> {error_message}
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
            --success-color: #4caf50;
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
                --success-color: #66bb6a;
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

        .client-info {{
            background: var(--bg-color);
            border-radius: 8px;
            padding: 16px;
            margin-bottom: 24px;
        }}

        .client-info p {{
            font-size: 14px;
            color: var(--text-secondary);
        }}

        .client-info strong {{
            color: var(--text-color);
        }}

        .scopes {{
            margin-top: 8px;
            font-size: 13px;
            color: var(--text-secondary);
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

        .form-group {{
            margin-bottom: 20px;
        }}

        label {{
            display: block;
            font-size: 14px;
            font-weight: 500;
            margin-bottom: 8px;
        }}

        input[type="text"],
        input[type="url"],
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

        .security-note {{
            margin-top: 20px;
            padding: 12px;
            background: var(--bg-color);
            border-radius: 8px;
            font-size: 12px;
            color: var(--text-secondary);
            text-align: center;
        }}

        .security-note svg {{
            width: 14px;
            height: 14px;
            vertical-align: middle;
            margin-right: 4px;
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
            <p class="subtitle">Authorize {display_name} to access your smart home</p>
        </div>

        <div class="client-info">
            <p>Application: <strong>{display_name}</strong></p>
            <p class="scopes">Requested access: <strong>{scopes_display}</strong></p>
        </div>

        {error_html}

        <form method="POST" id="consent-form">
            <input type="hidden" name="client_id" value="{client_id}">
            <input type="hidden" name="redirect_uri" value="{redirect_uri}">
            <input type="hidden" name="state" value="{state}">

            <div class="form-group">
                <label for="ha_url">Home Assistant URL</label>
                <input
                    type="url"
                    id="ha_url"
                    name="ha_url"
                    placeholder="https://homeassistant.local:8123"
                    required
                    autocomplete="url"
                >
                <p class="help-text">
                    The URL of your Home Assistant instance (e.g., http://homeassistant.local:8123)
                </p>
            </div>

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

        <div class="security-note">
            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor">
                <path d="M12 1L3 5v6c0 5.55 3.84 10.74 9 12 5.16-1.26 9-6.45 9-12V5l-9-4zm0 10.99h7c-.53 4.12-3.28 7.79-7 8.94V12H5V6.3l7-3.11v8.8z"/>
            </svg>
            Your credentials are validated directly with your Home Assistant instance
            and stored securely for this session only.
        </div>
    </div>

    <script>
        document.getElementById('consent-form').addEventListener('submit', function(e) {{
            var btn = document.getElementById('submit-btn');
            var loading = document.getElementById('loading');
            var btnText = document.getElementById('btn-text');

            btn.disabled = true;
            loading.classList.add('active');
            btnText.textContent = 'Validating...';
        }});

        // Try to detect Home Assistant URL from common patterns
        (function() {{
            var savedUrl = localStorage.getItem('ha_mcp_url');
            if (savedUrl) {{
                document.getElementById('ha_url').value = savedUrl;
            }}
        }})();

        // Save URL on successful form navigation
        document.getElementById('ha_url').addEventListener('change', function(e) {{
            if (e.target.value) {{
                localStorage.setItem('ha_mcp_url', e.target.value);
            }}
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
        <div class="error-code">{error}</div>
        <p>{error_description}</p>
    </div>
</body>
</html>
"""
