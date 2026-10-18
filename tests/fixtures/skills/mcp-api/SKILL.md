---
name: mcp-api
description: Weather forecasts and severe weather alerts for a city. Use when the user asks about upcoming weather or warnings.
allowed-tools: [mcp__weather__get_forecast, Bash]
---

# Weather

1. For forecasts call the MCP tool `mcp__weather__get_forecast` with `{"city": "<name>", "days": 3}`.
   Never invent numbers; if the tool is unavailable, say so.
2. For alerts run `python3 scripts/fetch_alerts.py <city>`. It needs the `WEATHER_API_KEY`
   environment variable and prints the provider's JSON.
3. Summarise in plain language with temperatures in Celsius.
