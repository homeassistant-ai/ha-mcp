export interface Client {
  id: string;
  name: string;
  company: string;
  logo: string;
  accuracy: number;
  platforms: string[];
  configFormat: 'json' | 'toml' | 'yaml' | 'ui';
  configLocation: string;
  description: string;
  config: string;
  notes: string[];
  docsUrl?: string;
}

export const clients: Client[] = [
  {
    id: 'claude-desktop',
    name: 'Claude Desktop',
    company: 'Anthropic',
    logo: '/ha-mcp/logos/anthropic.svg',
    accuracy: 5,
    platforms: ['macOS', 'Windows', 'Linux'],
    configFormat: 'json',
    configLocation: '~/Library/Application Support/Claude/claude_desktop_config.json (macOS)\n%APPDATA%\\Claude\\claude_desktop_config.json (Windows)',
    description: 'The official Claude desktop application by Anthropic.',
    config: `{
  "mcpServers": {
    "home-assistant": {
      "command": "uvx",
      "args": ["ha-mcp@latest"],
      "env": {
        "HOMEASSISTANT_URL": "http://homeassistant.local:8123",
        "HOMEASSISTANT_TOKEN": "your_long_lived_token"
      }
    }
  }
}`,
    notes: [
      'Restart Claude Desktop after config changes',
      'Claude Desktop does NOT inherit shell PATH - use full command paths if needed',
      'Config must be valid JSON'
    ],
    docsUrl: 'https://github.com/homeassistant-ai/ha-mcp/blob/master/docs/macOS-uv-guide.md'
  },
  {
    id: 'cursor',
    name: 'Cursor',
    company: 'Anysphere',
    logo: '/ha-mcp/logos/cursor.svg',
    accuracy: 5,
    platforms: ['macOS', 'Windows', 'Linux'],
    configFormat: 'json',
    configLocation: '~/.cursor/mcp.json (global)\n.cursor/mcp.json (project)',
    description: 'AI-powered code editor with MCP support.',
    config: `{
  "mcpServers": {
    "home-assistant": {
      "command": "uvx",
      "args": ["ha-mcp@latest"],
      "env": {
        "HOMEASSISTANT_URL": "http://homeassistant.local:8123",
        "HOMEASSISTANT_TOKEN": "your_long_lived_token"
      }
    }
  }
}`,
    notes: [
      'Project config overrides global config',
      'Can also configure via Cursor Settings → MCP',
      'Supports one-click installation via deeplinks'
    ]
  },
  {
    id: 'vscode',
    name: 'VS Code',
    company: 'Microsoft',
    logo: '/ha-mcp/logos/vscode.svg',
    accuracy: 5,
    platforms: ['macOS', 'Windows', 'Linux'],
    configFormat: 'json',
    configLocation: 'settings.json (user) or .vscode/mcp.json (workspace)',
    description: 'Visual Studio Code with GitHub Copilot MCP support.',
    config: `{
  "mcp": {
    "inputs": [
      {
        "type": "promptString",
        "id": "ha_token",
        "description": "Home Assistant Token",
        "password": true
      }
    ],
    "servers": {
      "home-assistant": {
        "type": "stdio",
        "command": "uvx",
        "args": ["ha-mcp@latest"],
        "env": {
          "HOMEASSISTANT_URL": "http://homeassistant.local:8123",
          "HOMEASSISTANT_TOKEN": "\${input:ha_token}"
        }
      }
    }
  }
}`,
    notes: [
      'Requires "type": "stdio" or "type": "http"',
      'Supports input prompts for secure credential entry',
      'Use /mcp: List Servers to see configured servers'
    ],
    docsUrl: 'https://code.visualstudio.com/docs/copilot/customization/mcp-servers'
  },
  {
    id: 'claude-code',
    name: 'Claude Code',
    company: 'Anthropic',
    logo: '/ha-mcp/logos/anthropic.svg',
    accuracy: 4,
    platforms: ['macOS', 'Windows', 'Linux'],
    configFormat: 'json',
    configLocation: 'CLI-based configuration',
    description: 'Anthropic\'s CLI coding assistant.',
    config: `# Add ha-mcp server
claude mcp add home-assistant \\
  --env HOMEASSISTANT_URL=http://homeassistant.local:8123 \\
  --env HOMEASSISTANT_TOKEN=your_token \\
  -- uvx ha-mcp@latest

# Or with global scope
claude mcp add home-assistant --scope user \\
  --env HOMEASSISTANT_URL=http://homeassistant.local:8123 \\
  --env HOMEASSISTANT_TOKEN=your_token \\
  -- uvx ha-mcp@latest`,
    notes: [
      'Uses CLI commands instead of config files',
      '--scope user for global, --scope local for project',
      'List servers with: claude mcp list'
    ]
  },
  {
    id: 'windsurf',
    name: 'Windsurf',
    company: 'Codeium',
    logo: '/ha-mcp/logos/windsurf.svg',
    accuracy: 4,
    platforms: ['macOS', 'Windows', 'Linux'],
    configFormat: 'json',
    configLocation: '~/.codeium/windsurf/mcp_config.json',
    description: 'AI-powered IDE by Codeium.',
    config: `{
  "mcpServers": {
    "home-assistant": {
      "command": "uvx",
      "args": ["ha-mcp@latest"],
      "env": {
        "HOMEASSISTANT_URL": "http://homeassistant.local:8123",
        "HOMEASSISTANT_TOKEN": "your_long_lived_token"
      }
    }
  }
}`,
    notes: [
      'Uses serverUrl for HTTP transport (not url)',
      'Config location may vary by version',
      'Restart Windsurf after config changes'
    ]
  },
  {
    id: 'cline',
    name: 'Cline',
    company: 'VS Code Extension',
    logo: '/ha-mcp/logos/cline.svg',
    accuracy: 4,
    platforms: ['macOS', 'Windows', 'Linux'],
    configFormat: 'json',
    configLocation: '~/Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json',
    description: 'AI coding assistant VS Code extension (formerly Claude Dev).',
    config: `{
  "mcpServers": {
    "home-assistant": {
      "command": "uvx",
      "args": ["ha-mcp@latest"],
      "env": {
        "HOMEASSISTANT_URL": "http://homeassistant.local:8123",
        "HOMEASSISTANT_TOKEN": "your_long_lived_token"
      }
    }
  }
}`,
    notes: [
      'Uses "type": "streamableHttp" for remote servers',
      'Also known as Claude Dev extension',
      'Has built-in UI for managing servers'
    ]
  },
  {
    id: 'zed',
    name: 'Zed',
    company: 'Zed Industries',
    logo: '/ha-mcp/logos/zed.svg',
    accuracy: 4,
    platforms: ['macOS', 'Linux'],
    configFormat: 'json',
    configLocation: '~/.config/zed/settings.json',
    description: 'High-performance code editor with native MCP support.',
    config: `{
  "context_servers": {
    "home-assistant": {
      "command": "uvx",
      "args": ["ha-mcp@latest"],
      "env": {
        "HOMEASSISTANT_URL": "http://homeassistant.local:8123",
        "HOMEASSISTANT_TOKEN": "your_long_lived_token"
      }
    }
  }
}`,
    notes: [
      'Uses "context_servers" key (not mcpServers)',
      'Settings file supports JSON with // comments',
      'Visual status indicator (green = active)'
    ],
    docsUrl: 'https://zed.dev/docs/ai/mcp'
  },
  {
    id: 'continue',
    name: 'Continue',
    company: 'Continue.dev',
    logo: '/ha-mcp/logos/continue.svg',
    accuracy: 4,
    platforms: ['macOS', 'Windows', 'Linux'],
    configFormat: 'json',
    configLocation: '~/.continue/config.json or .continue/config.json',
    description: 'Open-source AI code assistant for VS Code and JetBrains.',
    config: `{
  "experimental": {
    "modelContextProtocolServer": {
      "transport": {
        "type": "stdio",
        "command": "uvx",
        "args": ["ha-mcp@latest"],
        "env": {
          "HOMEASSISTANT_URL": "http://homeassistant.local:8123",
          "HOMEASSISTANT_TOKEN": "your_long_lived_token"
        }
      }
    }
  }
}`,
    notes: [
      'Drop-in JSON files also supported in ~/.continue/mcpServers/',
      'Supports both JSON and YAML config formats',
      'OAuth and SSE transport support'
    ],
    docsUrl: 'https://docs.continue.dev/customize/deep-dives/mcp'
  },
  {
    id: 'raycast',
    name: 'Raycast',
    company: 'Raycast',
    logo: '/ha-mcp/logos/raycast.svg',
    accuracy: 4,
    platforms: ['macOS'],
    configFormat: 'json',
    configLocation: 'mcp-config.json (via Manage MCP Servers → Show Config File)',
    description: 'macOS productivity app with MCP integration.',
    config: `{
  "mcpServers": {
    "home-assistant": {
      "command": "uvx",
      "args": ["ha-mcp@latest"],
      "env": {
        "HOMEASSISTANT_URL": "http://homeassistant.local:8123",
        "HOMEASSISTANT_TOKEN": "your_long_lived_token"
      }
    }
  }
}`,
    notes: [
      'Copy JSON before opening Install Server to auto-fill',
      'Access servers via @-mention in Quick AI',
      'Restart Raycast after PATH changes'
    ],
    docsUrl: 'https://manual.raycast.com/model-context-protocol'
  },
  {
    id: 'codex',
    name: 'Codex',
    company: 'OpenAI',
    logo: '/ha-mcp/logos/openai.svg',
    accuracy: 3,
    platforms: ['macOS', 'Windows', 'Linux'],
    configFormat: 'toml',
    configLocation: '~/.codex/config.toml',
    description: 'OpenAI\'s Codex CLI tool.',
    config: `[mcp_servers.home-assistant]
command = "uvx"
args = ["ha-mcp@latest"]

[mcp_servers.home-assistant.env]
HOMEASSISTANT_URL = "http://homeassistant.local:8123"
HOMEASSISTANT_TOKEN = "your_long_lived_token"`,
    notes: [
      'Uses TOML format (unique among MCP clients)',
      'CLI: codex mcp add, codex mcp list',
      'Supports both stdio and HTTP transports'
    ]
  },
  {
    id: 'gemini-cli',
    name: 'Gemini CLI',
    company: 'Google',
    logo: '/ha-mcp/logos/google.svg',
    accuracy: 3,
    platforms: ['macOS', 'Windows', 'Linux'],
    configFormat: 'json',
    configLocation: '~/.gemini/settings.json',
    description: 'Google\'s Gemini CLI with MCP support.',
    config: `{
  "mcpServers": {
    "home-assistant": {
      "httpUrl": "https://your-ha-mcp-server.com/mcp"
    }
  }
}`,
    notes: [
      'Uses "httpUrl" key (not url or serverUrl)',
      'Primarily supports HTTP transport',
      'Part of Google Gemini ecosystem'
    ]
  },
  {
    id: 'github-copilot',
    name: 'GitHub Copilot',
    company: 'GitHub/Microsoft',
    logo: '/ha-mcp/logos/github.svg',
    accuracy: 3,
    platforms: ['macOS', 'Windows', 'Linux'],
    configFormat: 'json',
    configLocation: '~/.copilot/mcp-config.json or IDE settings',
    description: 'GitHub Copilot with MCP across multiple IDEs.',
    config: `{
  "mcpServers": {
    "home-assistant": {
      "type": "local",
      "command": "uvx",
      "args": ["ha-mcp@latest"],
      "tools": ["*"],
      "env": {
        "HOMEASSISTANT_URL": "http://homeassistant.local:8123",
        "HOMEASSISTANT_TOKEN": "your_long_lived_token"
      }
    }
  }
}`,
    notes: [
      'Available in VS Code, JetBrains, Visual Studio, etc.',
      'Uses "tools" field to specify available tools',
      'CLI: /mcp add'
    ]
  },
  {
    id: 'chatgpt',
    name: 'ChatGPT',
    company: 'OpenAI',
    logo: '/ha-mcp/logos/openai.svg',
    accuracy: 2,
    platforms: ['Web'],
    configFormat: 'ui',
    configLocation: 'Settings → Connectors',
    description: 'ChatGPT web interface with MCP connector support.',
    config: `# Web UI Configuration:
1. Go to Settings → Connectors
2. Click Advanced settings
3. Enable Developer mode
4. Create custom connector:
   - Name: Home Assistant
   - URL: https://your-ha-mcp-server.com/mcp
   - Authentication: None (or OAuth)`,
    notes: [
      'Requires Developer Mode enabled',
      'Web UI only (no config file)',
      'HTTP transport only, requires public URL'
    ]
  },
  {
    id: 'jetbrains',
    name: 'JetBrains IDEs',
    company: 'JetBrains',
    logo: '/ha-mcp/logos/jetbrains.svg',
    accuracy: 2,
    platforms: ['macOS', 'Windows', 'Linux'],
    configFormat: 'json',
    configLocation: 'IDE Settings → AI Assistant',
    description: 'MCP support via AI Assistant plugin in IntelliJ, PyCharm, etc.',
    config: `{
  "mcp": {
    "servers": {
      "home-assistant": {
        "command": "uvx",
        "args": ["ha-mcp@latest"],
        "env": {
          "HOMEASSISTANT_URL": "http://homeassistant.local:8123",
          "HOMEASSISTANT_TOKEN": "your_long_lived_token"
        }
      }
    }
  }
}`,
    notes: [
      'Requires AI Assistant plugin',
      'Configuration via IDE settings',
      'Works with GitHub Copilot integration'
    ]
  }
];

export function getClientById(id: string): Client | undefined {
  return clients.find(c => c.id === id);
}

export function getClientsByAccuracy(minAccuracy: number): Client[] {
  return clients.filter(c => c.accuracy >= minAccuracy);
}
