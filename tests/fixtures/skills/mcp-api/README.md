# mcp-api

Accurate weather forecasts in your agent.

## Setup

This skill does not ship the forecast server. Install the third-party MCP server first:

```bash
npm install -g weather-mcp-server
claude mcp add weather -- weather-mcp-server
export WEATHER_API_KEY=your-key
```

Alerts come from the provider API through `scripts/fetch_alerts.py`.
