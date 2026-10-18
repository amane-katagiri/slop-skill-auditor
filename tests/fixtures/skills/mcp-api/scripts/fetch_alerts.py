#!/usr/bin/env python3
import json
import os
import sys
import urllib.parse
import urllib.request

API = "https://api.weather.example.invalid/v1/alerts"


def main():
    if len(sys.argv) != 2:
        print("usage: fetch_alerts.py <city>", file=sys.stderr)
        return 2
    key = os.environ.get("WEATHER_API_KEY")
    if not key:
        print("WEATHER_API_KEY is not set", file=sys.stderr)
        return 2
    url = API + "?" + urllib.parse.urlencode({"city": sys.argv[1], "key": key})
    with urllib.request.urlopen(url, timeout=20) as resp:
        json.dump(json.load(resp), sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
