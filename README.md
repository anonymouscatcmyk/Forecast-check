# NIMH forecast check – daily data collector

Collects the next-day temperature forecasts published by NIMH (meteo.bg) and
the observed extremes (Stringmeteo) once a day, so the two can be compared.
Runs on GitHub Actions.

It is planned to show this information on a web page in the future; this
README will be updated when that happens.

## What it does each day (13:30 Sofia time)

1. Reads three tables on meteo.bg:
   - **Прогноза по градове** (`/bg/forecast/gradove`): min/max by town.
   - **В планините** (`/bg/forecast/planini`) and **По Черноморието** (`/bg/forecast/moreto`).
2. Adds tomorrow's forecast to `data/days.json`. Only the next-day forecast is collected.
3. Reads Stringmeteo's monthly extremes and fills in the observations for every past
   day that has none yet (it waits until at least 80 % of stations have both values).
4. Archives the raw pages (`archive/raw/<date>/`) and the forecast map image
   (`archive/maps/<date>_PrognozaBG_1.png`) so every value can be checked later.

Output files:

| File | Contents |
|---|---|
| `data/days.json` | One entry per day: forecast (`f`) and observations (`o`) per station, as `[min, max]` |
| `data/forecasts.csv` | Every published next-day value, including points that cannot be verified |
| `data/observations.csv` | The observations used for the comparison |

A forecast counts as a hit when it is within 2 °C of the observation.

## If something breaks

- **Run is red at "Check the parsers"**: the code was changed and broke; undo the change.
- **Run is red at "Collect"**: open the run log. Usually meteo.bg or Stringmeteo changed their
  page layout, or refused the request. The archived raw page shows what arrived.
- **"meteo.bg not updated yet"**: the forecast was not published at run time; start the
  workflow by hand later (Actions tab → *Run workflow*).

## Test locally (optional)

```
pip install requests beautifulsoup4 pytest
python -m pytest -q tests
python scripts/collect.py --offline tests/fixtures --today 2026-11-08   # uses saved pages, no internet
```
